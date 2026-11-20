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
   removed (refused again);
3. publishes the guidance again with the same DedupKey (updated, not added);
4. dismisses and mutes as the test user, and mutes and unmutes a category on
   every meeting (put back as it was);
5. publishes with one DedupKey until the meeting's per-minute allowance is used;
6. runs the transcript deletion function for the meeting (the guidance goes,
   the other category stays), then deletes the meeting through the meeting
   controls function (every notification row goes).

Every call names that meeting; it touches no other, and whatever is left is
removed by its CallId at the end. The result is a dict of observations.
See docs/meeting-notifications.md.
"""

from __future__ import annotations

import json
import os
import sys
import time
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

LAYER = Path(__file__).resolve().parents[1] / "lma-ai-stack" / "source" / "lambda_layers" / "configuration_layer"
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

    def __init__(self, deployment: Deployment, id_token: str, username: str, notifications: Any):
        self.d = deployment
        self.id_token = id_token
        self.username = username
        self.n = notifications
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        self.call_id = f"integ-test-notifications-{stamp}-{uuid.uuid4().hex[:8]}"
        self.owner = f"integ-notifications-{uuid.uuid4().hex[:8]}@example.com"
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
    user = _claims(tokens["id"]).get("cognito:username") or username

    with _publisher(deployment) as notifications:
        probe = Probe(deployment, tokens["id"], user, notifications)
        try:
            probe.create_meeting()
            probe.publish_two()
            probe.shared_reads()
            probe.dedup()
            probe.user_view()
            probe.burst()
            probe.unshared_reads()
            probe.transcript_deletion()
            probe.meeting_deletion()
        finally:
            probe.cleanup()
    return probe.observed
