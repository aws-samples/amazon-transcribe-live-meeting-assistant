# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Live guidance on a live stack, end to end.

Creates two synthetic meetings owned by the Cognito test user (START events on
the CallDataStream, as kds_pipeline_probe sends), each with the recorded
8-item checklist of the analyzer's unit tests (one item's status set by a
user), and sends both the recorded transcript as finished segments:

* the **guided** meeting starts with ``GuidanceEnabled: true``;
* the **control** meeting starts with ``GuidanceEnabled: false``.

The transcript goes in two waves, the second a little over the shipped 90-second
interval after the first, so that the call event processor queues a run that
sees the first wave whole. Then the probe waits for the guided meeting's runs
and records what they did: the analyzer's state row, the checklist, the
guidance notifications and the metering records. It asks for a digest as the
test user (requestMeetingDigest), and records the control meeting's state row
and metering, which should be absent.

The model's judgement varies from run to run, so the observations are what a
test can assert loosely: a covered or partial item with assistant evidence, a
guidance item that cites a checklist item. Every call names one of the two
meetings, and whatever is left of them is removed by CallId at the end.
See docs/live-guidance.md.
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import boto3
import cognito_test_user
from boto3.dynamodb.types import TypeDeserializer
from mcp_api_key_probe import ProbeError, _graphql, _graphql_url
from mcp_meeting_tools_probe import _claims
from ws_stream_probe import _mint_tokens, _outputs, _stack_resources

FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "lma-ai-stack/source/lambda_functions/live_guidance_analyzer/fixtures/renewal_meeting.json"
)
MEETING_WAIT_SECONDS = 90
#: A little over the shipped Seconds between runs.
SECOND_WAVE_AFTER_SECONDS = 100
RUN_WAIT_SECONDS = 300
USER_SET_ITEM = "q-notice-period"

DIGEST = """
mutation D($callId: ID!) {
  requestMeetingDigest(CallId: $callId) { Success Message Text NotificationId }
}
"""

DESERIALIZER = TypeDeserializer()


def _plain(item: dict) -> dict:
    return {key: DESERIALIZER.deserialize(value) for key, value in item.items()}


class Deployment:
    def __init__(self, stack_name: str, region: str | None):
        self.session = boto3.Session(region_name=region)
        cfn = self.session.client("cloudformation")
        self.outputs = _outputs(cfn, stack_name)
        ai_stack = next(
            r["PhysicalResourceId"] for r in _stack_resources(cfn, stack_name) if r["LogicalResourceId"] == "AISTACK"
        )
        ai = {r["LogicalResourceId"]: r["PhysicalResourceId"] for r in _stack_resources(cfn, ai_stack)}
        self.table = ai["EventSourcingTable"]
        self.graphql_url = _graphql_url(self.outputs)
        self.ddb = self.session.client("dynamodb")
        self.kinesis = self.session.client("kinesis")


class Meeting:
    """One synthetic meeting: its START, its segments, and reads of its rows."""

    def __init__(self, deployment: Deployment, owner: str, guided: bool):
        self.d = deployment
        self.owner = owner
        self.guided = guided
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        kind = "guided" if guided else "control"
        self.call_id = f"integ-test-guidance-{kind}-{stamp}-{uuid.uuid4().hex[:8]}"
        self.recorded = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.list_key: dict | None = None

    def _put(self, record: dict) -> None:
        self.d.kinesis.put_record(
            StreamName=self.d.outputs["CallDataStreamName"],
            PartitionKey=self.call_id,
            Data=json.dumps(record).encode(),
        )

    def start(self) -> None:
        now = datetime.now(timezone.utc).isoformat()
        checklist = []
        for item in self.recorded["meeting"]["Checklist"]:
            entry = {key: item[key] for key in ("ItemId", "Text", "Kind", "Priority", "Status")}
            entry.update(Evidence=[], UpdatedBy=self.owner, UpdatedAt=now)
            if item["ItemId"] == USER_SET_ITEM:
                entry.update(Status="partial", StatusSource="user")
            checklist.append(entry)
        self._put(
            {
                "EventType": "START",
                "CallId": self.call_id,
                "CustomerPhoneNumber": "Integ Test Participant",
                "SystemPhoneNumber": "LMA Integ Test",
                # With no access token the processor makes AgentId the Owner.
                "AgentId": self.owner,
                "CreatedAt": now,
                "AccessToken": "",
                "IdToken": "",
                "RefreshToken": "",
                "Agenda": self.recorded["meeting"]["Agenda"],
                "AgendaSource": "scheduled",
                "AgendaUpdatedBy": self.owner,
                "AgendaUpdatedAt": now,
                "MeetingContext": self.recorded["meeting"]["MeetingContext"],
                "MeetingContextUpdatedBy": self.owner,
                "MeetingContextUpdatedAt": now,
                "Labels": self.recorded["meeting"]["Labels"],
                "MeetingMetadataUpdatedAt": now,
                "Checklist": checklist,
                "ChecklistUpdatedAt": now,
                "GuidanceEnabled": self.guided,
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

    def send_segments(self, segments: list[dict]) -> None:
        now = datetime.now(timezone.utc).isoformat()
        for segment in segments:
            self._put(
                {
                    "EventType": "ADD_TRANSCRIPT_SEGMENT",
                    "CallId": self.call_id,
                    "Channel": segment["Channel"],
                    "SegmentId": f"{self.call_id}-{segment['SegmentId']}",
                    "StartTime": segment["StartTime"],
                    "EndTime": segment["EndTime"],
                    "Transcript": segment["Transcript"],
                    "IsPartial": False,
                    "CreatedAt": now,
                    "UpdatedAt": now,
                    "Speaker": segment["Speaker"],
                    "AccessToken": "",
                    "IdToken": "",
                    "RefreshToken": "",
                }
            )

    def end(self) -> None:
        self._put({"EventType": "END", "CallId": self.call_id, "AccessToken": "", "IdToken": "", "RefreshToken": ""})

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

    def state(self) -> dict | None:
        rows = [_plain(r) for r in self.partition(f"gd#{self.call_id}") if r["SK"]["S"] == "analyzer"]
        return rows[0] if rows else None

    def guidance(self) -> list[dict]:
        rows = [_plain(r) for r in self.partition(f"nt#{self.call_id}") if r["SK"]["S"].startswith("n#")]
        return [r for r in rows if r.get("Category") == "guidance"]

    def metering_contexts(self) -> list[str]:
        return sorted({_plain(r).get("Context") for r in self.partition(f"mtr#{self.call_id}")} - {None})

    def cleanup(self) -> None:
        for prefix in ("c#", "trs#", "nt#", "gd#", "mtr#"):
            for item in self.partition(f"{prefix}{self.call_id}"):
                self.d.ddb.delete_item(TableName=self.d.table, Key={"PK": item["PK"], "SK": item["SK"]})
        if self.list_key:
            self.d.ddb.delete_item(TableName=self.d.table, Key=self.list_key)


def _summarise(meeting: Meeting) -> dict[str, Any]:
    row = meeting.meeting() or {}
    state = meeting.state() or {}
    return {
        "checklist": [
            {
                "ItemId": item.get("ItemId"),
                "Status": item.get("Status"),
                "StatusSource": item.get("StatusSource"),
                "Evidence": item.get("Evidence") or [],
            }
            for item in row.get("Checklist") or []
        ],
        "guidance_enabled": row.get("GuidanceEnabled"),
        "run_count": int(state.get("RunCount") or 0),
        "has_state": bool(state),
        "has_summary": bool(state.get("RollingSummary")),
        "guidance": [
            {
                "Type": g.get("Type"),
                "Source": g.get("Source"),
                "Severity": g.get("Severity"),
                "Title": g.get("Title"),
                "Body": g.get("Body"),
                "Refs": g.get("Refs") or [],
                "DedupKey": g.get("DedupKey"),
                "ExpiresAt": g.get("ExpiresAt"),
                "Revision": int(g.get("Revision") or 0),
            }
            for g in meeting.guidance()
        ],
        "metering_contexts": meeting.metering_contexts(),
    }


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
    owner = _claims(tokens["id"]).get("cognito:username") or username

    guided = Meeting(deployment, owner, guided=True)
    control = Meeting(deployment, owner, guided=False)
    observed: dict[str, Any] = {"guided_call_id": guided.call_id, "control_call_id": control.call_id}
    segments = guided.recorded["segments"]
    try:
        for meeting in (guided, control):
            meeting.start()
        for meeting in (guided, control):
            if not meeting.wait_created():
                raise ProbeError(f"meeting {meeting.call_id} was not created; see test_kds_pipeline_creates_meeting")
        first, second = segments[:16], segments[16:]
        for meeting in (guided, control):
            meeting.send_segments(first)
        time.sleep(SECOND_WAVE_AFTER_SECONDS)
        for meeting in (guided, control):
            meeting.send_segments(second)

        deadline = time.monotonic() + RUN_WAIT_SECONDS
        while time.monotonic() < deadline:
            summary = _summarise(guided)
            changed = [i for i in summary["checklist"] if i["StatusSource"] == "assistant"]
            if summary["run_count"] >= 1 and (changed or summary["guidance"]):
                break
            time.sleep(10)
        observed["guided"] = _summarise(guided)

        try:
            data = _graphql(deployment.graphql_url, tokens["id"], DIGEST, {"callId": guided.call_id})
            observed["digest"] = data["requestMeetingDigest"]
        except ProbeError as error:
            observed["digest"] = {"Success": False, "Message": f"request failed: {error}"}
        observed["guided_after_digest"] = _summarise(guided)
        observed["control"] = _summarise(control)
    finally:
        for meeting in (guided, control):
            try:
                meeting.end()
            except Exception:  # noqa: BLE001 - cleanup below removes the rows either way
                pass
        for meeting in (guided, control):
            meeting.cleanup()
    return observed


if __name__ == "__main__":
    import sys

    print(json.dumps(run_probe(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None), indent=1, default=str))
