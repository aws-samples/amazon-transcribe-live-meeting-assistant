# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Meeting whiteboards on a live stack, end to end.

Creates two synthetic meetings owned by the Cognito test user (START events on
the CallDataStream, as kds_pipeline_probe sends):

* the **assisted** meeting starts with ``WhiteboardsEnabled: true``;
* the **control** meeting starts with ``WhiteboardsEnabled: false``, and gets a
  whiteboard made by hand.

The assisted meeting is sent a recorded planning conversation in two waves, the
second a little over the shipped 180 seconds between runs after the first. After
the first run has filled the default whiteboard, the test user rewrites its
Decisions panel; the probe then records whether the second run left that panel
as the user wrote it. It reads the whiteboards over the hosted MCP server with a
read-write key (and checks a read key is not offered the write tools), reads the
meeting export, runs the transcript deletion function on the assisted meeting,
and records the metering of both meetings.

The model's wording varies from run to run, so the observations are what a test
can assert loosely: a panel the assistant filled, not its text. Every call names
one of the two meetings, the API keys are revoked, and whatever is left of the
meetings is removed by CallId at the end. See docs/meeting-whiteboards.md.
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import boto3
import cognito_test_user
from boto3.dynamodb.types import TypeDeserializer
from mcp_api_key_probe import REVOKE_KEY, ProbeError, _graphql, _graphql_url, _post
from mcp_meeting_tools_probe import GENERATE_KEY, _claims
from ws_stream_probe import _mint_tokens, _outputs, _stack_resources

MEETING_WAIT_SECONDS = 90
#: A little over the shipped Seconds between runs (180).
SECOND_WAVE_AFTER_SECONDS = 200
RUN_WAIT_SECONDS = 300
USER_DECISION = "- Launch moves to the second week of March (set by the integration test)"

CONVERSATION = [
    ("Ana", "Thanks for joining. Today we need to settle the launch date and the pricing tiers."),
    ("Ben", "Engineering can be ready by the first week of March if the payments work lands."),
    ("Ana", "Then let's decide: we launch on March fourth, assuming payments are done."),
    ("Ben", "Agreed, March fourth. I'll own the payments integration."),
    (
        "Ana",
        "For pricing we have three options: a free tier, a team tier at ten dollars, and enterprise.",
    ),
    ("Ben", "Do we know if enterprise includes single sign-on? That's still open."),
    ("Ana", "We don't know yet. Let's keep that as an open question for Priya."),
    ("Ben", "The team tier should include up to twenty seats."),
    ("Ana", "Decision: the team tier is ten dollars per seat and includes up to twenty seats."),
    ("Ben", "One risk is the app store review, which can take two weeks."),
    ("Ana", "Good point. We should submit the build by February eighteenth."),
    ("Ben", "I'll note that. Who writes the launch announcement?"),
    ("Ana", "Marketing will, but we haven't confirmed who exactly. Open question."),
    ("Ben", "The process is: build, internal test, app store review, then launch."),
    ("Ana", "Yes, and after launch we monitor for a week before announcing broadly."),
    ("Ben", "So that's settled for the launch plan."),
    ("Ana", "Next, the support plan. Support hours will be nine to five on weekdays at first."),
    ("Ben", "Decision then: weekday support nine to five, weekends later."),
    ("Ana", "And we drop the free tier's file limit to one gigabyte."),
    ("Ben", "Agreed. The single sign-on question still needs Priya."),
    ("Ana", "Let's also compare annual and monthly billing next time."),
    ("Ben", "Fine, monthly only at launch then. That's a decision."),
    ("Ana", "Great. I think that covers it for today."),
    ("Ben", "Thanks everyone."),
]

_FIELDS = """
    CallId WhiteboardId Title Template AssistantUpdates Version CreatedBy UpdatedBy
    Owner SharedWith Panels { PanelId Title Kind Content Source UpdatedBy }
"""
LIST = (
    "query L($c: ID!) { listMeetingWhiteboards(CallId: $c) { WhiteboardsEnabled Writable Items {"
    + _FIELDS
    + "} } }"
)
CREATE = (
    "mutation C($i: CreateMeetingWhiteboardInput!) { createMeetingWhiteboard(input: $i) {"
    + _FIELDS
    + "} }"
)
UPDATE = (
    "mutation U($i: UpdateMeetingWhiteboardInput!) { updateMeetingWhiteboard(input: $i) {"
    + _FIELDS
    + "} }"
)
VERSIONS = (
    "query V($c: ID!, $w: ID!) { listMeetingWhiteboardVersions(CallId: $c, WhiteboardId: $w, Limit: 50) "
    "{ Items { Version Source UpdatedBy ChangedPanels } } }"
)

DESERIALIZER = TypeDeserializer()


def _plain(item: dict) -> dict:
    return {key: DESERIALIZER.deserialize(value) for key, value in item.items()}


class Deployment:
    def __init__(self, stack_name: str, region: str | None):
        self.session = boto3.Session(region_name=region)
        cfn = self.session.client("cloudformation")
        self.outputs = _outputs(cfn, stack_name)
        ai_stack = next(
            r["PhysicalResourceId"]
            for r in _stack_resources(cfn, stack_name)
            if r["LogicalResourceId"] == "AISTACK"
        )
        ai = {
            r["LogicalResourceId"]: r["PhysicalResourceId"] for r in _stack_resources(cfn, ai_stack)
        }
        self.table = ai["EventSourcingTable"]
        self.deletion_function = ai.get("TranscriptDeletionFunction")
        self.endpoint = self.outputs.get("MCPServerApiKeyEndpoint")
        self.graphql_url = _graphql_url(self.outputs)
        self.ddb = self.session.client("dynamodb")
        self.kinesis = self.session.client("kinesis")
        self.lam = self.session.client("lambda")


class Meeting:
    """One synthetic meeting: its START, its segments, and reads of its rows."""

    def __init__(self, deployment: Deployment, owner: str, assisted: bool):
        self.d = deployment
        self.owner = owner
        self.assisted = assisted
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        kind = "assisted" if assisted else "control"
        self.call_id = f"integ-test-whiteboards-{kind}-{stamp}-{uuid.uuid4().hex[:8]}"
        self.list_key: dict | None = None

    def _put(self, record: dict) -> None:
        self.d.kinesis.put_record(
            StreamName=self.d.outputs["CallDataStreamName"],
            PartitionKey=self.call_id,
            Data=json.dumps(record).encode(),
        )

    def start(self) -> None:
        self._put(
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
                "Agenda": "Launch date\nPricing tiers\nSupport plan",
                "AgendaSource": "scheduled",
                "WhiteboardsEnabled": self.assisted,
            }
        )

    def wait_created(self) -> bool:
        deadline = time.monotonic() + MEETING_WAIT_SECONDS
        while time.monotonic() < deadline:
            row = self.meeting()
            if row:
                created = row["CreatedAt"]
                self.list_key = {
                    "PK": {"S": f"cls#{created[:10]}#s#{int(created[11:13]) // 4:02d}"},
                    "SK": {"S": f"ts#{created}#id#{self.call_id}"},
                }
                return True
            time.sleep(3)
        return False

    def send(self, lines: list[tuple[str, str]], offset: int) -> None:
        now = datetime.now(timezone.utc).isoformat()
        for number, (speaker, text) in enumerate(lines, start=offset):
            self._put(
                {
                    "EventType": "ADD_TRANSCRIPT_SEGMENT",
                    "CallId": self.call_id,
                    "Channel": "CALLER" if speaker == "Ana" else "AGENT",
                    "SegmentId": f"{self.call_id}-s{number}",
                    "StartTime": number * 6.0,
                    "EndTime": number * 6.0 + 5.0,
                    "Transcript": text,
                    "IsPartial": False,
                    "CreatedAt": now,
                    "UpdatedAt": now,
                    "Speaker": speaker,
                    "AccessToken": "",
                    "IdToken": "",
                    "RefreshToken": "",
                }
            )

    def end(self) -> None:
        self._put(
            {
                "EventType": "END",
                "CallId": self.call_id,
                "AccessToken": "",
                "IdToken": "",
                "RefreshToken": "",
            }
        )

    def meeting(self) -> dict | None:
        key = {"PK": {"S": f"c#{self.call_id}"}, "SK": {"S": f"c#{self.call_id}"}}
        item = self.d.ddb.get_item(TableName=self.d.table, Key=key, ConsistentRead=True).get("Item")
        return _plain(item) if item else None

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

    def state(self) -> dict:
        rows = [
            _plain(r) for r in self.partition(f"wb#{self.call_id}") if r["SK"]["S"] == "updater"
        ]
        return rows[0] if rows else {}

    def whiteboard_rows(self) -> list[dict]:
        return [_plain(r) for r in self.partition(f"wb#{self.call_id}")]

    def metering_contexts(self) -> list[str]:
        return sorted(
            {_plain(r).get("Context") for r in self.partition(f"mtr#{self.call_id}")} - {None}
        )

    def cleanup(self) -> None:
        for prefix in ("c#", "trs#", "nt#", "wb#", "mtr#", "gd#"):
            for item in self.partition(f"{prefix}{self.call_id}"):
                self.d.ddb.delete_item(
                    TableName=self.d.table, Key={"PK": item["PK"], "SK": item["SK"]}
                )
        if self.list_key:
            self.d.ddb.delete_item(TableName=self.d.table, Key=self.list_key)


class Probe:
    def __init__(self, deployment: Deployment, id_token: str):
        self.d = deployment
        self.id_token = id_token

    def gql(self, query: str, variables: dict) -> dict:
        return _graphql(self.d.graphql_url, self.id_token, query, variables)

    def boards(self, meeting: Meeting) -> list[dict]:
        return self.gql(LIST, {"c": meeting.call_id})["listMeetingWhiteboards"]["Items"]

    def mcp(self, key: str, name: str, arguments: dict) -> tuple[bool, Any]:
        status, body = _post(
            self.d.endpoint,
            {"x-api-key": key},
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
        )
        if status != 200 or not isinstance(body, dict) or "result" not in body:
            raise ProbeError(f"{name} returned HTTP {status}: {json.dumps(body)[:300]}")
        text = body["result"]["content"][0]["text"]
        return (True, text) if body["result"].get("isError") else (False, json.loads(text))

    def tool_names(self, key: str) -> set[str]:
        status, body = _post(
            self.d.endpoint, {"x-api-key": key}, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        )
        if status != 200:
            raise ProbeError(f"tools/list returned HTTP {status}")
        return {tool["name"] for tool in body["result"]["tools"]}

    def key(self, scope: str) -> tuple[str, str]:
        generated = self.gql(GENERATE_KEY, {"scope": scope})["generateMCPApiKey"]
        return generated["keyValue"], generated["keyPrefix"]

    def revoke(self, prefix: str) -> None:
        self.gql(REVOKE_KEY, {"p": prefix})


def _wait_runs(meeting: Meeting, count: int) -> dict:
    deadline = time.monotonic() + RUN_WAIT_SECONDS
    while time.monotonic() < deadline:
        state = meeting.state()
        if int(state.get("RunCount") or 0) >= count and "LeaseUntil" not in state:
            return state
        time.sleep(10)
    return meeting.state()


def _panels(board: dict) -> list[dict]:
    return [
        {
            "PanelId": p["PanelId"],
            "Title": p["Title"],
            "Kind": p["Kind"],
            "Content": p.get("Content") or "",
        }
        for p in board.get("Panels") or []
    ]


def run_probe(stack_name: str, region: str | None = None) -> dict:  # noqa: C901 - one scenario, in order
    """Run the scenario and report what it observed."""
    credentials = cognito_test_user.resolve(region)
    if credentials is None:
        raise cognito_test_user.CredentialsUnavailable(
            f"no Cognito test user configured — {cognito_test_user.describe_sources()}"
        )
    deployment = Deployment(stack_name, region)
    username, password = credentials
    outputs = deployment.outputs
    pool_id = (
        outputs.get("MCPServerUserPoolId")
        or outputs["CognitoUserPoolTokenIssuerUrl"].rsplit("/", 1)[-1]
    )
    tokens = _mint_tokens(
        deployment.session.region_name,
        pool_id,
        outputs["CognitoUserPoolClientId"],
        username,
        password,
    )
    owner = _claims(tokens["id"]).get("cognito:username") or username
    probe = Probe(deployment, tokens["id"])

    assisted = Meeting(deployment, owner, assisted=True)
    control = Meeting(deployment, owner, assisted=False)
    observed: dict[str, Any] = {
        "assisted_call_id": assisted.call_id,
        "control_call_id": control.call_id,
    }
    first, second = CONVERSATION[:16], CONVERSATION[16:]
    try:
        for meeting in (assisted, control):
            meeting.start()
        for meeting in (assisted, control):
            if not meeting.wait_created():
                raise ProbeError(
                    f"meeting {meeting.call_id} was not created; see test_kds_pipeline_creates_meeting"
                )
        observed["assisted_enabled"] = (assisted.meeting() or {}).get("WhiteboardsEnabled")
        observed["control_enabled"] = (control.meeting() or {}).get("WhiteboardsEnabled")

        # A whiteboard made by hand on the control meeting: no model call.
        manual = probe.gql(
            CREATE, {"i": {"CallId": control.call_id, "Title": "By hand", "Template": "blank"}}
        )
        manual = manual["createMeetingWhiteboard"]
        panels = _panels(manual)
        panels[0]["Content"] = "- typed by the integration test"
        edited = probe.gql(
            UPDATE,
            {
                "i": {
                    "CallId": control.call_id,
                    "WhiteboardId": manual["WhiteboardId"],
                    "BaseVersion": manual["Version"],
                    "Panels": panels,
                }
            },
        )
        observed["manual"] = edited["updateMeetingWhiteboard"]

        assisted.send(first, 0)
        control.send(first, 0)
        state = _wait_runs(assisted, 1)
        observed["first_state"] = {k: state.get(k) for k in ("RunCount", "LastSegmentEndTime")}
        observed["after_first"] = probe.boards(assisted)

        # The user takes the Decisions panel.
        board = observed["after_first"][0] if observed["after_first"] else None
        if board:
            panels = _panels(board)
            for panel in panels:
                if panel["Title"] == "Decisions":
                    panel["Content"] = USER_DECISION
            taken = probe.gql(
                UPDATE,
                {
                    "i": {
                        "CallId": assisted.call_id,
                        "WhiteboardId": board["WhiteboardId"],
                        "BaseVersion": board["Version"],
                        "Panels": panels,
                    }
                },
            )
            observed["after_user_edit"] = taken["updateMeetingWhiteboard"]
            # The stale version is refused.
            try:
                probe.gql(
                    UPDATE,
                    {
                        "i": {
                            "CallId": assisted.call_id,
                            "WhiteboardId": board["WhiteboardId"],
                            "BaseVersion": board["Version"],
                            "Title": "stale",
                        }
                    },
                )
                observed["stale_refused"] = False
            except ProbeError as error:
                observed["stale_refused"] = "WhiteboardConflict" in str(error) or "changed" in str(
                    error
                )

        time.sleep(SECOND_WAVE_AFTER_SECONDS)
        assisted.send(second, len(first))
        control.send(second, len(first))
        state = _wait_runs(assisted, 2)
        observed["second_state"] = {k: state.get(k) for k in ("RunCount",)}
        observed["after_second"] = probe.boards(assisted)
        if board:
            observed["versions"] = probe.gql(
                VERSIONS, {"c": assisted.call_id, "w": board["WhiteboardId"]}
            )["listMeetingWhiteboardVersions"]["Items"]

        if deployment.endpoint and board:
            read_key, read_prefix = probe.key("read")
            try:
                observed["read_key_tools"] = sorted(
                    n for n in probe.tool_names(read_key) if "whiteboard" in n
                )
            finally:
                probe.revoke(read_prefix)
            key, prefix = probe.key("read-write")
            try:
                observed["rw_key_tools"] = sorted(
                    n for n in probe.tool_names(key) if "whiteboard" in n
                )
                _, observed["mcp_list"] = probe.mcp(
                    key, "list_meeting_whiteboards", {"meetingId": assisted.call_id}
                )
                _, current = probe.mcp(
                    key,
                    "get_meeting_whiteboard",
                    {"meetingId": assisted.call_id, "whiteboardId": board["WhiteboardId"]},
                )
                observed["mcp_get"] = current
                _, observed["mcp_not_modified"] = probe.mcp(
                    key,
                    "get_meeting_whiteboard",
                    {
                        "meetingId": assisted.call_id,
                        "whiteboardId": board["WhiteboardId"],
                        "ifNewerThan": current["version"],
                    },
                )
                _, export = probe.mcp(
                    key,
                    "get_meeting_export",
                    {"meetingId": assisted.call_id, "include": ["whiteboards"]},
                )
                observed["export_whiteboards"] = export.get("whiteboards")
            finally:
                probe.revoke(prefix)

        if deployment.deletion_function:
            response = deployment.lam.invoke(
                FunctionName=deployment.deletion_function,
                Payload=json.dumps({"CallId": assisted.call_id, "Trigger": "scheduled"}).encode(),
            )
            observed["deletion_error"] = response.get("FunctionError")
            observed["rows_after_deletion"] = len(assisted.whiteboard_rows())

        observed["assisted_metering"] = assisted.metering_contexts()
        observed["control_metering"] = control.metering_contexts()
        observed["control_state"] = control.state()
    finally:
        for meeting in (assisted, control):
            try:
                meeting.end()
            except Exception:  # noqa: BLE001 - cleanup below removes the rows either way
                pass
        for meeting in (assisted, control):
            meeting.cleanup()
    return observed


if __name__ == "__main__":
    import sys

    print(
        json.dumps(
            run_probe(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None),
            indent=1,
            default=str,
        )
    )
