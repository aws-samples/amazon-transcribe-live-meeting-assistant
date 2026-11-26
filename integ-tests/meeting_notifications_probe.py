# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Meeting notifications on a live stack, end to end.

Creates one synthetic meeting owned by a synthetic user (a START event on the
CallDataStream, as kds_pipeline_probe sends), so that the Cognito test user can
see it only once it is shared with them. Then:

1. publishes a guidance notification and a notification of another category
   with ``lma_notifications.publish_meeting_notification``, signed with the
   runner's IAM credentials, as a backend function would;
2. reads them as the test user before the meeting is shared with them (refused),
   after it is shared through the meeting controls function as the meeting page
   shares it (both earlier notifications returned), and after the share is
   removed (refused again). An Admin reads every meeting, so the observations
   record whether the test user is one (``test_user_is_admin``), and the two
   refusals are checked only for a test user who is not;
3. publishes the guidance again with the same DedupKey (updated, not added);
4. dismisses and mutes as the test user, and mutes and unmutes a category on
   every meeting (put back as it was);
5. publishes with one DedupKey until the meeting's per-minute allowance is used;
6. runs the transcript deletion function for the meeting (the guidance goes,
   the other category stays), then deletes the meeting through the meeting
   controls function (every notification row goes).

Live delivery is checked as the web UI receives it: once the meeting is shared,
the test user subscribes over the AppSync real-time endpoint with the UI's own
subscription document (lma-ai-stack/source/ui/src/graphql/queries/
onMeetingNotification.js), once for the meeting and once for every meeting, and
a publish reaches both. A second synthetic meeting, owned by the test user and
with no notifications, is used the same way, and first, where Playwright and
its Chromium are installed, in the web UI: with the meeting list open a publish
raises the top navigation's count, and with the meeting's page then opened by a
change of route (no reload), the meeting's first notification opens its
Notifications drawer.

Every call names one of those meetings; it touches no other, and whatever is
left is removed by its CallId at the end. The result is a dict of observations.
See docs/meeting-notifications.md.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import sys
import time
import urllib.parse
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import boto3
import cognito_test_user
from mcp_api_key_probe import ProbeError, _graphql, _graphql_url
from mcp_meeting_tools_probe import _claims
from ws_stream_probe import _mint_tokens, _outputs, _stack_resources

REPO = Path(__file__).resolve().parents[1]
LAYER = REPO / "lma-ai-stack" / "source" / "lambda_layers" / "configuration_layer"
# The subscription the web UI makes, read from the UI so the probe asks for what it asks for.
UI_SUBSCRIPTION = REPO / "lma-ai-stack" / "source" / "ui" / "src" / "graphql" / "queries" / "onMeetingNotification.js"
LIVE_WAIT_SECONDS = 30
PAGE_WAIT_MS = 30000
MEETING_WAIT_SECONDS = 90
DELETE_WAIT_SECONDS = 120
RATE_PER_MINUTE = 120
# A category name no feature publishes, used for the every-meeting mute round trip.
PROBE_CATEGORY = "integ-probe"

LIST = """
query List($callId: ID!, $category: String) {
  listMeetingNotifications(CallId: $callId, Category: $category, Limit: 100) {
    Items { NotificationId Category Type Severity Title Body Refs DedupKey Revision CreatedAt UpdatedAt }
    NextToken
  }
}
"""
GET_STATE = "query S($c: ID!) { getMyMeetingNotificationState(CallId: $c) { Dismissed MutedCategories } }"
UPDATE_STATE = """
mutation U($input: UpdateMyMeetingNotificationStateInput!) {
  updateMyMeetingNotificationState(input: $input) { Dismissed MutedCategories }
}
"""
GET_PREFS = "query P { getMyNotificationPreferences { MutedCategories } }"
UPDATE_PREFS = """
mutation M($input: UpdateMyNotificationPreferencesInput!) {
  updateMyNotificationPreferences(input: $input) { MutedCategories }
}
"""


class Deployment:
    """What the probe needs from the stack, resolved once."""

    def __init__(self, stack_name: str, region: str | None):
        self.session = boto3.Session(region_name=region)
        cfn = self.session.client("cloudformation")
        self.outputs = _outputs(cfn, stack_name)
        ai_stack = next(
            r["PhysicalResourceId"] for r in _stack_resources(cfn, stack_name) if r["LogicalResourceId"] == "AISTACK"
        )
        ai = {r["LogicalResourceId"]: r["PhysicalResourceId"] for r in _stack_resources(cfn, ai_stack)}
        self.table = ai["EventSourcingTable"]
        self.controls_function = ai["MeetingControlsResolverFunction"]
        self.deletion_function = ai["TranscriptDeletionFunction"]
        self.graphql_url = _graphql_url(self.outputs)
        self.ddb = self.session.client("dynamodb")
        self.kinesis = self.session.client("kinesis")
        self.lam = self.session.client("lambda")


@contextmanager
def _publisher(deployment: Deployment):
    """The publish helper from the configuration layer, pointed at this stack."""
    if str(LAYER) not in sys.path:
        sys.path.insert(0, str(LAYER))
    import lma_notifications  # pylint: disable=import-outside-toplevel

    saved = {name: os.environ.get(name) for name in ("APPSYNC_GRAPHQL_URL", "AWS_REGION")}
    os.environ["APPSYNC_GRAPHQL_URL"] = deployment.graphql_url
    os.environ["AWS_REGION"] = deployment.session.region_name or ""
    try:
        yield lma_notifications
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


class Probe:
    """One run of the scenario; observations go in ``self.observed``."""

    def __init__(
        self, deployment: Deployment, id_token: str, username: str, notifications: Any, owner: str | None = None
    ):
        self.d = deployment
        self.id_token = id_token
        self.username = username
        self.n = notifications
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        self.call_id = f"integ-test-notifications-{stamp}-{uuid.uuid4().hex[:8]}"
        self.owner = owner or f"integ-notifications-{uuid.uuid4().hex[:8]}@example.com"
        self.observed: dict[str, Any] = {"call_id": self.call_id}
        self.list_keys: dict[str, str] = {}

    # ── the meeting ────────────────────────────────────────────────────────

    def create_meeting(self) -> None:
        self.d.kinesis.put_record(
            StreamName=self.d.outputs["CallDataStreamName"],
            PartitionKey=self.call_id,
            Data=json.dumps(
                {
                    "EventType": "START",
                    "CallId": self.call_id,
                    "CustomerPhoneNumber": "Integ Test Participant",
                    "SystemPhoneNumber": "LMA Integ Test",
                    # With no access token the processor makes AgentId the Owner.
                    "AgentId": self.owner,
                    "CreatedAt": datetime.now(timezone.utc).isoformat(),
                    "AccessToken": "",
                    "IdToken": "",
                    "RefreshToken": "",
                }
            ).encode(),
        )
        deadline = time.monotonic() + MEETING_WAIT_SECONDS
        while time.monotonic() < deadline:
            row = self.meeting_row()
            if row:
                created = row["CreatedAt"]["S"]
                shard = int(created[11:13]) // 4
                self.list_keys = {
                    "ListPK": f"cls#{created[:10]}#s#{shard:02d}",
                    "ListSK": f"ts#{created}#id#{self.call_id}",
                }
                return
            time.sleep(3)
        raise ProbeError("the synthetic meeting was not created; see test_kds_pipeline_creates_meeting")

    def meeting_row(self) -> dict | None:
        key = {"PK": {"S": f"c#{self.call_id}"}, "SK": {"S": f"c#{self.call_id}"}}
        return self.d.ddb.get_item(TableName=self.d.table, Key=key, ConsistentRead=True).get("Item")

    def partition(self, pk: str) -> list[dict]:
        items, start = [], None
        while True:
            kwargs = {
                "TableName": self.d.table,
                "KeyConditionExpression": "PK = :pk",
                "ExpressionAttributeValues": {":pk": {"S": pk}},
                "ConsistentRead": True,
            }
            if start:
                kwargs["ExclusiveStartKey"] = start
            page = self.d.ddb.query(**kwargs)
            items.extend(page.get("Items", []))
            start = page.get("LastEvaluatedKey")
            if not start:
                return items

    def _controls(self, field: str, extra: dict) -> Any:
        event = {
            "identity": {"username": self.owner, "groups": []},
            "arguments": {"input": {"Calls": [{"CallId": self.call_id, **self.list_keys}], **extra}},
            "info": {"fieldName": field},
        }
        response = self.d.lam.invoke(
            FunctionName=self.d.controls_function, Payload=json.dumps(event).encode()
        )
        return json.loads(response["Payload"].read() or b"null")

    def share(self, recipients: str) -> Any:
        """Share as the meeting page does: the owner's shareMeetings, which replaces the list."""
        return self._controls("shareMeetings", {"MeetingRecipients": recipients})

    def cleanup(self) -> None:
        """Remove whatever is left of this probe's meeting, by its CallId only."""
        for pk in (f"c#{self.call_id}", f"trs#{self.call_id}", f"nt#{self.call_id}"):
            for item in self.partition(pk):
                self.d.ddb.delete_item(TableName=self.d.table, Key={"PK": item["PK"], "SK": item["SK"]})
        if self.list_keys:
            self.d.ddb.delete_item(
                TableName=self.d.table,
                Key={"PK": {"S": self.list_keys["ListPK"]}, "SK": {"S": self.list_keys["ListSK"]}},
            )

    # ── as the test user ───────────────────────────────────────────────────

    def read(self, category: str | None = None) -> list[dict] | None:
        """The test user's read of the meeting's notifications, or None when refused."""
        try:
            data = _graphql(self.d.graphql_url, self.id_token, LIST, {"callId": self.call_id, "category": category})
        except ProbeError as error:
            self.observed.setdefault("refusals", []).append(str(error)[:200])
            return None
        return data["listMeetingNotifications"]["Items"]

    def user(self, query: str, variables: dict | None = None) -> dict | None:
        try:
            return _graphql(self.d.graphql_url, self.id_token, query, variables)
        except ProbeError as error:
            self.observed.setdefault("refusals", []).append(str(error)[:200])
            return None

    # ── the scenario ───────────────────────────────────────────────────────

    def publish(self, **arguments: Any) -> dict:
        return self.n.publish_meeting_notification(self.call_id, source="integ-test", **arguments)

    def publish_two(self) -> None:
        guidance = self.publish(
            category="guidance",
            type="ask_question",
            severity="suggestion",
            title="Ask who signs off on the budget",
            body="Nobody has named **an owner** yet.",
            refs=["checklist:c1", "segment:seg-1"],
            dedup_key="ask_question:c1",
            actions=[{"label": "Copy question", "kind": "copy", "payload": {"text": "Who signs off?"}}],
        )
        status = self.publish(category="status", type="note", title="Another category")
        self.observed["guidance_id"] = guidance.get("NotificationId")
        self.observed["status_id"] = status.get("NotificationId")
        self.observed["published_created"] = [guidance.get("Created"), status.get("Created")]
        self.observed["published_owner"] = guidance.get("Owner")
        self.observed["published_shared_with_before_share"] = guidance.get("SharedWith")

    def shared_reads(self) -> None:
        self.observed["read_before_share"] = self.read()
        self.observed["share_result"] = self.share(self.username)
        items = self.read() or []
        self.observed["read_after_share_ids"] = [item["NotificationId"] for item in items]
        self.observed["read_after_share"] = items
        self.observed["read_guidance_only"] = [item["Category"] for item in self.read("guidance") or []]

    def live(self, title: str) -> dict:
        """Subscribe as the test user, as the web UI does, publish once, and return what arrived."""
        return live_delivery(
            self.d,
            self.id_token,
            self.call_id,
            lambda: self.publish(category="status", type="live", title=title),
        )

    def dedup(self) -> None:
        again = self.publish(
            category="guidance",
            type="ask_question",
            severity="warning",
            title="Ask who signs off on the budget (time is short)",
            dedup_key="ask_question:c1",
        )
        self.observed["dedup_created"] = again.get("Created")
        self.observed["dedup_id"] = again.get("NotificationId")
        self.observed["dedup_revision"] = again.get("Revision")
        self.observed["dedup_shared_with"] = again.get("SharedWith")
        self.observed["guidance_after_dedup"] = self.read("guidance") or []

    def user_view(self) -> None:
        guidance_id = self.observed["guidance_id"]
        self.user(UPDATE_STATE, {"input": {"CallId": self.call_id, "Dismiss": [guidance_id]}})
        self.user(UPDATE_STATE, {"input": {"CallId": self.call_id, "Mute": ["status"]}})
        state = (self.user(GET_STATE, {"c": self.call_id}) or {}).get("getMyMeetingNotificationState") or {}
        self.observed["state"] = state
        before = ((self.user(GET_PREFS) or {}).get("getMyNotificationPreferences") or {}).get("MutedCategories") or []
        muted = self.user(UPDATE_PREFS, {"input": {"Mute": [PROBE_CATEGORY]}}) or {}
        self.observed["prefs_muted"] = (muted.get("updateMyNotificationPreferences") or {}).get("MutedCategories")
        if PROBE_CATEGORY not in before:
            unmuted = self.user(UPDATE_PREFS, {"input": {"Unmute": [PROBE_CATEGORY]}}) or {}
            self.observed["prefs_restored"] = (unmuted.get("updateMyNotificationPreferences") or {}).get(
                "MutedCategories"
            )
        else:
            self.observed["prefs_restored"] = before

    def burst(self) -> None:
        # Start at the top of the next minute, so the burst has a counter of its
        # own: the earlier publishes counted against the current one.
        time.sleep(60 - datetime.now(timezone.utc).second + 1)
        started = time.monotonic()
        accepted, refused = 0, None
        for _ in range(RATE_PER_MINUTE + 5):
            try:
                self.publish(category="status", type="burst", title="Burst", dedup_key="burst")
                accepted += 1
            except self.n.NotificationRateLimited as error:
                refused = str(error)
                break
        self.observed["burst_accepted"] = accepted
        self.observed["burst_refused"] = refused
        self.observed["burst_seconds"] = time.monotonic() - started

    def unshared_reads(self) -> None:
        self.observed["unshare_result"] = self.share("")
        self.observed["read_after_unshare"] = self.read()
        self.observed["state_after_unshare"] = self.user(
            UPDATE_STATE, {"input": {"CallId": self.call_id, "Restore": [self.observed["guidance_id"]]}}
        )

    def transcript_deletion(self) -> None:
        response = self.d.lam.invoke(
            FunctionName=self.d.deletion_function,
            Payload=json.dumps({"CallId": self.call_id, "Trigger": "scheduled"}).encode(),
        )
        self.observed["deletion_error"] = response.get("FunctionError")
        rows = self.partition(f"nt#{self.call_id}")
        self.observed["categories_after_transcript_deletion"] = sorted(
            {row.get("Category", {}).get("S") for row in rows if row["SK"]["S"][:2] in ("n#", "d#")}
        )
        self.observed["user_rows_after_transcript_deletion"] = sum(
            1 for row in rows if row["SK"]["S"].startswith("u#")
        )
        try:
            self.publish(category="guidance", type="note", title="After the transcript")
            self.observed["guidance_after_deletion"] = "accepted"
        except self.n.NotificationTranscriptDeleted:
            self.observed["guidance_after_deletion"] = "refused"
        except self.n.NotificationError as error:
            self.observed["guidance_after_deletion"] = f"other: {error.error_type}"

    def meeting_deletion(self) -> None:
        self.observed["delete_result"] = self._controls("deleteMeetings", {})
        deadline = time.monotonic() + DELETE_WAIT_SECONDS
        while time.monotonic() < deadline and self.meeting_row():
            time.sleep(3)
        self.observed["meeting_after_delete"] = self.meeting_row() is not None
        self.observed["rows_after_delete"] = len(self.partition(f"nt#{self.call_id}"))


# ── live delivery ──────────────────────────────────────────────────────────


def ui_subscription() -> str:
    """The web UI's onMeetingNotification subscription document."""
    match = re.search(r"`([^`]*)`", UI_SUBSCRIPTION.read_text())
    if not match:
        raise ProbeError(f"no subscription document in {UI_SUBSCRIPTION}")
    return match.group(1)


def realtime_url(graphql_url: str) -> str | None:
    """The AppSync real-time endpoint of a GraphQL URL, or None for a custom domain."""
    if ".appsync-api." not in graphql_url:
        return None
    return graphql_url.replace("https://", "wss://").replace(".appsync-api.", ".appsync-realtime-api.")


async def _live(url: str, host: str, token: str, call_id: str, publish) -> dict:
    import websockets  # pylint: disable=import-outside-toplevel

    auth = {"host": host, "Authorization": token}
    header = base64.b64encode(json.dumps(auth).encode()).decode()
    payload = base64.b64encode(b"{}").decode()
    query = ui_subscription()
    # The meeting page subscribes for its meeting; the top navigation for every meeting.
    subscriptions = {"meeting": {"callId": call_id}, "every": {}}
    seen: dict = {"acks": {}, "received": {name: [] for name in subscriptions}}
    async with websockets.connect(f"{url}?header={header}&payload={payload}", subprotocols=["graphql-ws"]) as ws:
        await ws.send(json.dumps({"type": "connection_init"}))
        await asyncio.wait_for(ws.recv(), timeout=20)
        for name, variables in subscriptions.items():
            start = {
                "data": json.dumps({"query": query, "variables": variables}),
                "extensions": {"authorization": auth},
            }
            await ws.send(json.dumps({"id": name, "type": "start", "payload": start}))
        deadline = time.monotonic() + 20
        while len(seen["acks"]) < len(subscriptions) and time.monotonic() < deadline:
            message = json.loads(await asyncio.wait_for(ws.recv(), timeout=20))
            if message.get("type") in ("start_ack", "error") and message.get("id") in subscriptions:
                seen["acks"][message["id"]] = message["type"] if message["type"] == "start_ack" else message
        if sorted(seen["acks"]) != sorted(subscriptions) or any(a != "start_ack" for a in seen["acks"].values()):
            return seen
        published = await asyncio.get_running_loop().run_in_executor(None, publish)
        seen["published_id"] = published.get("NotificationId")
        deadline = time.monotonic() + LIVE_WAIT_SECONDS
        def has_published(name: str) -> bool:
            return any(item.get("NotificationId") == seen["published_id"] for item in seen["received"][name])

        # Until each has the publish itself: an Admin's every-meeting subscription
        # also receives other meetings' notifications.
        while time.monotonic() < deadline and not all(has_published(name) for name in subscriptions):
            try:
                message = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            except asyncio.TimeoutError:
                continue
            if message.get("type") == "data" and message.get("id") in subscriptions:
                seen["received"][message["id"]].append(message["payload"]["data"]["onMeetingNotification"])
            elif message.get("type") == "error":
                seen.setdefault("errors", []).append(message)
    return seen


def live_delivery(deployment: Deployment, id_token: str, call_id: str, publish) -> dict:
    """What the UI's two subscriptions received for one publish, or why it was not checked."""
    try:
        import websockets  # noqa: F401  # pylint: disable=import-outside-toplevel,unused-import
    except ImportError:
        return {"skipped": "websockets not installed (pip install -r integ-tests/requirements.txt)"}
    url = realtime_url(deployment.graphql_url)
    if not url:
        return {"skipped": "the GraphQL endpoint is not an AppSync domain; its real-time endpoint is not derived"}
    try:
        return asyncio.run(_live(url, deployment.graphql_url.split("/")[2], id_token, call_id, publish))
    except Exception as error:  # noqa: BLE001 — reported by its own test, not the whole scenario
        return {"error": f"{type(error).__name__}: {str(error)[:200]}"}


def page_delivery(listed: Probe, opened: Probe, app_url: str, username: str, password: str) -> dict:
    """The web UI, signed in as the test user, as two notifications are published.

    The first is published to ``listed``'s meeting with the meeting list open:
    the top navigation's count shows it. Then ``opened``'s meeting, which has
    no notifications, is opened by a change of route (not a reload), as a link
    in LMA opens it, and its first notification is published: its Notifications
    drawer opens with it.
    """
    try:
        from playwright.sync_api import sync_playwright  # pylint: disable=import-outside-toplevel
    except ImportError:
        return {"skipped": "playwright not installed (pip install -r integ-tests/requirements-video.txt)"}
    on_page = "Published on the meeting page"
    seen: dict = {}
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                page = browser.new_context(viewport={"width": 1500, "height": 1000}).new_page()
                page.goto(f"{app_url.rstrip('/')}/#/calls")
                page.get_by_label(re.compile("^(Username|Email)$")).first.fill(username)
                page.get_by_label("Password", exact=True).fill(password)
                page.get_by_role("button", name="Sign in").click()
                page.wait_for_load_state("networkidle")
                bell = (
                    page.locator("#top-navigation")
                    .get_by_role("button", name=re.compile("^Meeting notifications"))
                    .first
                )
                bell.wait_for(timeout=PAGE_WAIT_MS)
                # Time for the page's subscriptions to be acknowledged.
                page.wait_for_timeout(5000)
                listed.publish(category="status", type="live", title="Published with the meeting list open")
                try:
                    page.wait_for_function(
                        "() => [...document.querySelectorAll('#top-navigation button')]"
                        ".some((b) => /during this visit/.test(b.getAttribute('aria-label') || ''))",
                        timeout=PAGE_WAIT_MS,
                    )
                except Exception as error:  # noqa: BLE001 — what the bell says is recorded below
                    seen["bell_wait"] = str(error)[:200]
                seen["bell_on_list"] = bell.get_attribute("aria-label")
                page.evaluate(
                    "(hash) => { window.location.hash = hash; }",
                    f"#/calls/{urllib.parse.quote(opened.call_id, safe='')}",
                )
                page.get_by_text(opened.call_id).first.wait_for(timeout=PAGE_WAIT_MS)
                page.wait_for_timeout(5000)
                seen["drawer_triggers_before"] = [
                    button.get_attribute("aria-label")
                    for button in page.get_by_role("button", name=re.compile("^Notifications")).all()
                ]
                opened.publish(category="status", type="live", title=on_page)
                try:
                    page.get_by_text(on_page).first.wait_for(state="visible", timeout=PAGE_WAIT_MS)
                    seen["drawer_opened"] = True
                except Exception:  # noqa: BLE001 — reported by the test
                    seen["drawer_opened"] = False
                seen["drawer_triggers_after"] = [
                    button.get_attribute("aria-label")
                    for button in page.get_by_role("button", name=re.compile("^Notifications")).all()
                ]
            finally:
                browser.close()
    except Exception as error:  # noqa: BLE001 — reported by its own test, not the whole scenario
        # Only a missing browser is a skip.
        if "Executable doesn't exist" in str(error):
            return {"skipped": "playwright's Chromium is not installed (python -m playwright install chromium)"}
        return {**seen, "error": f"{type(error).__name__}: {str(error)[:200]}"}
    return seen


def run_probe(stack_name: str, region: str | None = None) -> dict:
    """Run the scenario and report what it observed."""
    credentials = cognito_test_user.resolve(region)
    if credentials is None:
        raise cognito_test_user.CredentialsUnavailable(
            f"no Cognito test user configured — {cognito_test_user.describe_sources()}"
        )
    deployment = Deployment(stack_name, region)
    username, password = credentials
    outputs = deployment.outputs
    pool_id = outputs.get("MCPServerUserPoolId") or outputs["CognitoUserPoolTokenIssuerUrl"].rsplit("/", 1)[-1]
    tokens = _mint_tokens(deployment.session.region_name, pool_id, outputs["CognitoUserPoolClientId"], username, password)
    claims = _claims(tokens["id"])
    user = claims.get("cognito:username") or username
    # An Admin reads every meeting, so the refusals to a user a meeting is not shared with cannot be seen.
    is_admin = "Admin" in (claims.get("cognito:groups") or [])

    with _publisher(deployment) as notifications:
        probe = Probe(deployment, tokens["id"], user, notifications)
        # A meeting of the test user's own, with no notifications until the page check.
        own = Probe(deployment, tokens["id"], user, notifications, owner=user)
        try:
            probe.create_meeting()
            own.create_meeting()
            probe.observed["own_call_id"] = own.call_id
            probe.observed["test_user_is_admin"] = is_admin
            probe.publish_two()
            probe.shared_reads()
            probe.observed["live_shared"] = probe.live("Published to a shared meeting")
            app_url = outputs.get("ApplicationCloudfrontEndpoint")
            probe.observed["page"] = (
                page_delivery(probe, own, app_url, username, password)
                if app_url
                else {"skipped": "the stack has no ApplicationCloudfrontEndpoint output"}
            )
            probe.observed["live_owned"] = own.live("Published to an owned meeting")
            probe.dedup()
            probe.user_view()
            probe.burst()
            probe.unshared_reads()
            probe.transcript_deletion()
            probe.meeting_deletion()
        finally:
            try:
                probe.cleanup()
            finally:
                own.cleanup()
    return probe.observed
