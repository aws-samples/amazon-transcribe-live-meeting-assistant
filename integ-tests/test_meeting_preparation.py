# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""A meeting's preparation -- labels, audience, meeting context and checklist -- end to end.

See docs/meeting-context-and-checklist.md for the behaviour asserted.

Default run (no meeting needed, about a minute, self-cleaning):

``test_a_scheduled_vp_stores_its_preparation_and_an_edit_before_it_starts``
    Schedules a Virtual Participant a day ahead with all four inputs, reads
    them back from its row, edits them with updateScheduledVirtualParticipant
    and reads the edits back; an input over a limit is refused. The row is
    cancelled at the end, so it never launches.
``test_a_meeting_created_with_its_preparation_has_it``
    Puts on the call data stream the START a Virtual Participant sends when it
    carries the four inputs, and reads them back from the meeting record the
    Call Event Processor creates.
``test_a_replace_keeps_progress_and_the_assistant_never_overrides_a_user``
    On that meeting: a status set for a user survives a replace of the
    checklist, the assistant cannot change it, and a reset starts it again.
``test_deleting_the_transcript_removes_the_evidence_quotes``
    Runs the transcript deletion function on that meeting, as a scheduled
    deletion does, and checks each quote is gone and its segment id and time
    are kept.

Opt-in, with ``LMA_TEST_VP_LIVE_MEETING=1`` and the meeting secret described in
``test_vp_live_meeting.py`` (marked ``external_meeting``, about ten minutes):

``test_a_vp_scheduled_and_edited_before_it_starts_creates_the_meeting_with_the_edits``
    Schedules a Virtual Participant six minutes ahead with all four inputs,
    edits them at once, waits for it to launch on its schedule and join, and
    reads the edited values back from the meeting record it created.

The backend calls here are signed with the runner's IAM credentials, as a
backend role's are, so an item update names its StatusSource.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any

import boto3
import live_meeting
import pytest
from kds_pipeline_probe import _event_sourcing_table, _stack_resources, _stream_name, cleanup
from live_meeting import LiveMeeting, wait_for
from lma_sdk import LMAClient
from lma_sdk.exceptions import LMAError

OWNER = "integ-test@lma.aws"

LABELS = [{"Key": "project", "Value": "integ-test"}, {"Key": "phase", "Value": "1"}]
CONTEXT = "# Goals\nAgree the integration test plan."
CHECKLIST = [
    {"ItemId": "q1", "Text": "Who owns the budget?", "Kind": "question", "Priority": "must"},
    {"ItemId": "r1", "Text": "Vendor lock-in", "Kind": "risk"},
]

VP_FIELDS = """
    id
    status
    labels { Key Value }
    audience
    meetingContext
    checklist { ItemId Text Kind Priority Source }
"""
CREATE_VP = f"""
mutation CreateVirtualParticipant($input: CreateVirtualParticipantInput!) {{
  createVirtualParticipant(input: $input) {{ {VP_FIELDS} }}
}}
"""
GET_VP = f"""
query GetVirtualParticipant($id: ID!) {{
  getVirtualParticipant(id: $id) {{ {VP_FIELDS} }}
}}
"""
EDIT_VP = f"""
mutation UpdateScheduledVirtualParticipant($input: UpdateScheduledVirtualParticipantInput!) {{
  updateScheduledVirtualParticipant(input: $input) {{ {VP_FIELDS} }}
}}
"""
CALL_FIELDS = """
    CallId
    Owner
    SharedWith
    Labels { Key Value }
    Audience
    MeetingMetadataUpdatedAt
    MeetingContext
    MeetingContextUpdatedAt
    Checklist {
      ItemId Text Kind Priority Source Status StatusSource Note UpdatedBy UpdatedAt
      Evidence { SegmentId StartTime Quote }
    }
    ChecklistUpdatedAt
"""
REPLACE_CHECKLIST = f"""
mutation UpdateCallChecklist($input: UpdateCallChecklistInput!) {{
  updateCallChecklist(input: $input) {{ {CALL_FIELDS} }}
}}
"""
UPDATE_ITEM = f"""
mutation UpdateCallChecklistItem($input: UpdateCallChecklistItemInput!) {{
  updateCallChecklistItem(input: $input) {{ {CALL_FIELDS} }}
}}
"""
UPDATE_LABELS = f"""
mutation UpdateCallLabels($input: UpdateCallLabelsInput!) {{
  updateCallLabels(input: $input) {{ {CALL_FIELDS} }}
}}
"""

MEETING_RECORD_TIMEOUT_S = 90.0


def _labels(row: dict[str, Any]) -> list[dict[str, str]]:
    return [{"Key": label["Key"], "Value": label["Value"]} for label in row.get("labels") or row.get("Labels") or []]


def _definitions(items: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    return [
        {key: item.get(key) for key in ("ItemId", "Text", "Kind", "Priority") if item.get(key) is not None}
        for item in items or []
    ]


def _end(client: LMAClient, vp_id: str | None, reason: str) -> None:
    """End a VP the test started. A reason naming "cancelled" records a VP that
    never launched as CANCELLED (end_virtual_participant's rule)."""
    if not vp_id:
        return
    try:
        client.vp.end(vp_id, reason=reason)
    except LMAError as err:
        print(f"could not end Virtual Participant {vp_id}: {err}")


@pytest.fixture(scope="module")
def aws(client: LMAClient) -> dict[str, Any]:
    session = boto3.session.Session(region_name=client.region) if client.region else boto3.session.Session()
    cfn = session.client("cloudformation")
    ddb = session.client("dynamodb")
    ai_stack = next(
        r["PhysicalResourceId"] for r in _stack_resources(cfn, client.stack_name) if r["LogicalResourceId"] == "AISTACK"
    )
    deletion_function = next(
        r["PhysicalResourceId"] for r in _stack_resources(cfn, ai_stack)
        if r["LogicalResourceId"] == "TranscriptDeletionFunction"
    )
    return {
        "kinesis": session.client("kinesis"),
        "lambda": session.client("lambda"),
        "ddb": ddb,
        "stream": _stream_name(cfn, client.stack_name),
        "table": _event_sourcing_table(cfn, ddb, client.stack_name),
        "deletion_function": deletion_function,
    }


# ── a scheduled Virtual Participant ─────────────────────────────────────────


def test_a_scheduled_vp_stores_its_preparation_and_an_edit_before_it_starts(client: LMAClient) -> None:
    vp_id = None
    try:
        created = client.appsync.graphql(
            CREATE_VP,
            {
                "input": {
                    "meetingName": "integ-test (meeting preparation, never launched)",
                    "meetingPlatform": "ZOOM",
                    "meetingId": "0000000000",
                    "isScheduled": True,
                    "status": "SCHEDULED",
                    "meetingTime": int(time.time()) + 24 * 3600,
                    "owner": OWNER,
                    "labels": LABELS,
                    "audience": "external",
                    "meetingContext": CONTEXT,
                    "checklist": CHECKLIST,
                }
            },
        )["createVirtualParticipant"]
        vp_id = created["id"]
        row = client.appsync.graphql(GET_VP, {"id": vp_id})["getVirtualParticipant"]
        assert _labels(row) == LABELS
        assert row["audience"] == "external"
        assert row["meetingContext"] == CONTEXT
        # Normalized as stored: the default priority is filled in.
        assert _definitions(row["checklist"]) == [
            {"ItemId": "q1", "Text": "Who owns the budget?", "Kind": "question", "Priority": "must"},
            {"ItemId": "r1", "Text": "Vendor lock-in", "Kind": "risk", "Priority": "should"},
        ]

        edited = client.appsync.graphql(
            EDIT_VP,
            {
                "input": {
                    "id": vp_id,
                    "labels": [{"Key": "project", "Value": "integ-test-edited"}],
                    "audience": "",
                    "checklist": [CHECKLIST[1]],
                }
            },
        )["updateScheduledVirtualParticipant"]
        assert edited["id"] == vp_id
        row = client.appsync.graphql(GET_VP, {"id": vp_id})["getVirtualParticipant"]
        assert _labels(row) == [{"Key": "project", "Value": "integ-test-edited"}]
        assert row["audience"] is None, "an audience sent empty is removed"
        assert row["meetingContext"] == CONTEXT, "an input left out of the edit is kept"
        assert [item["ItemId"] for item in row["checklist"]] == ["r1"]

        with pytest.raises(Exception, match="Kind must be one of"):
            client.appsync.graphql(
                EDIT_VP, {"input": {"id": vp_id, "checklist": [{"ItemId": "x", "Text": "t", "Kind": "idea"}]}}
            )
    finally:
        _end(client, vp_id, "integ-test meeting preparation: cancelled before launch")


# ── a meeting created with its preparation ──────────────────────────────────


@pytest.fixture(scope="module")
def prepared_meeting(client: LMAClient, aws: dict[str, Any]):
    """A meeting created from the START a Virtual Participant with preparation sends."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    call_id = f"integ-test-prep-{stamp}"
    now = datetime.now(timezone.utc).isoformat()
    record = {
        "EventType": "START",
        "CallId": call_id,
        "CustomerPhoneNumber": "Integ Test Participant",
        "SystemPhoneNumber": "LMA Integ Test",
        "AgentId": OWNER,
        "CreatedAt": now,
        "AccessToken": "",
        "IdToken": "",
        "RefreshToken": "",
        # What preparationStartFields (the VP's meeting-preparation.ts) adds.
        "Labels": LABELS,
        "Audience": "external",
        "MeetingMetadataUpdatedAt": now,
        "MeetingContext": CONTEXT,
        "MeetingContextUpdatedBy": OWNER,
        "MeetingContextUpdatedAt": now,
        "Checklist": [
            {**item, "Priority": item.get("Priority") or "should", "Status": "open", "Evidence": [],
             "UpdatedBy": OWNER, "UpdatedAt": now}
            for item in CHECKLIST
        ],
        "ChecklistUpdatedAt": now,
    }
    aws["kinesis"].put_record(StreamName=aws["stream"], Data=json.dumps(record).encode(), PartitionKey=call_id)
    meeting = wait_for(
        lambda: live_meeting.read_meeting(aws["ddb"], aws["table"], call_id),
        timeout=MEETING_RECORD_TIMEOUT_S,
        interval=3.0,
    )
    try:
        assert meeting, f"the START for {call_id} did not create a meeting within {MEETING_RECORD_TIMEOUT_S:.0f}s"
        yield call_id, meeting
    finally:
        cleanup(client.stack_name, call_id, region=client.region)


def test_a_meeting_created_with_its_preparation_has_it(prepared_meeting) -> None:
    _call_id, meeting = prepared_meeting
    assert _labels(meeting) == LABELS
    assert meeting["Audience"] == "external"
    assert meeting["MeetingContext"] == CONTEXT
    assert meeting["MeetingContextUpdatedBy"] == OWNER
    assert [item["ItemId"] for item in meeting["Checklist"]] == ["q1", "r1"]
    assert {item["Status"] for item in meeting["Checklist"]} == {"open"}
    assert meeting["ChecklistUpdatedAt"]


def test_a_replace_keeps_progress_and_the_assistant_never_overrides_a_user(
    client: LMAClient, prepared_meeting
) -> None:
    call_id, _meeting = prepared_meeting
    graphql = client.appsync.graphql
    graphql(UPDATE_ITEM, {"input": {"CallId": call_id, "ItemId": "q1", "Status": "covered",
                                    "StatusSource": "user", "UpdatedBy": OWNER}})
    replaced = graphql(
        REPLACE_CHECKLIST,
        {"input": {"CallId": call_id, "StatusSource": "user",
                   "Checklist": [CHECKLIST[1], CHECKLIST[0], {"ItemId": "c1", "Text": "Confirm the date", "Kind": "confirm"}]}},
    )["updateCallChecklist"]
    by_id = {item["ItemId"]: item for item in replaced["Checklist"]}
    assert [item["ItemId"] for item in replaced["Checklist"]] == ["r1", "q1", "c1"]
    assert (by_id["q1"]["Status"], by_id["q1"]["StatusSource"]) == ("covered", "user")
    assert by_id["c1"]["Status"] == "open"

    with pytest.raises(Exception, match="set by a user"):
        graphql(UPDATE_ITEM, {"input": {"CallId": call_id, "ItemId": "q1", "Status": "partial",
                                        "StatusSource": "assistant"}})

    assisted = graphql(
        UPDATE_ITEM,
        {"input": {"CallId": call_id, "ItemId": "r1", "Status": "partial", "StatusSource": "assistant",
                   "AddEvidence": [{"SegmentId": "seg-1", "StartTime": 12.5, "Quote": "we may be locked in"}]}},
    )["updateCallChecklistItem"]
    r1 = next(item for item in assisted["Checklist"] if item["ItemId"] == "r1")
    assert r1["StatusSource"] == "assistant"
    assert r1["Evidence"] == [{"SegmentId": "seg-1", "StartTime": 12.5, "Quote": "we may be locked in"}]

    cleared = graphql(UPDATE_LABELS, {"input": {"CallId": call_id, "Labels": []}})["updateCallLabels"]
    assert cleared["Labels"] == [] and cleared["MeetingMetadataUpdatedAt"]


def test_deleting_the_transcript_removes_the_evidence_quotes(
    client: LMAClient, aws: dict[str, Any], prepared_meeting
) -> None:
    call_id, _meeting = prepared_meeting
    client.appsync.graphql(
        UPDATE_ITEM,
        {"input": {"CallId": call_id, "ItemId": "q1", "StatusSource": "user",
                   "Evidence": [{"SegmentId": "seg-2", "StartTime": 30, "Quote": "Dana owns it"}]}},
    )
    response = aws["lambda"].invoke(
        FunctionName=aws["deletion_function"],
        Payload=json.dumps({"CallId": call_id, "Trigger": "scheduled"}).encode(),
    )
    assert "FunctionError" not in response, response["Payload"].read()
    meeting = live_meeting.read_meeting(aws["ddb"], aws["table"], call_id)
    assert meeting["TranscriptDeletedAt"]
    evidence = [entry for item in meeting["Checklist"] for entry in item.get("Evidence") or []]
    assert evidence, "the checklist had evidence before the deletion"
    assert all(entry.get("Quote") is None for entry in evidence)
    assert {"SegmentId": "seg-2", "StartTime": 30, "Quote": None} in [
        {**entry, "StartTime": float(entry["StartTime"])} for entry in evidence
    ]


# ── a Virtual Participant in a real meeting (opt-in) ────────────────────────

SCHEDULE_LEAD_S = 6 * 60
LAUNCH_AND_JOIN_TIMEOUT_S = SCHEDULE_LEAD_S + 600


@pytest.mark.external_meeting
@pytest.mark.skipif(
    not live_meeting.enabled(),
    reason=f"opt-in: set {live_meeting.ENABLE_VARIABLE}=1 and store the meeting in "
    f"{live_meeting.DEFAULT_SECRET_ID} (or name the secret in {live_meeting.SECRET_VARIABLE})",
)
def test_a_vp_scheduled_and_edited_before_it_starts_creates_the_meeting_with_the_edits(
    client: LMAClient, aws: dict[str, Any], region: str | None
) -> None:
    try:
        meeting: LiveMeeting = live_meeting.resolve(region)
    except live_meeting.LiveMeetingUnavailable as err:
        pytest.fail(str(err), pytrace=False)
    vp_id = None
    try:
        created = client.appsync.graphql(
            CREATE_VP,
            {
                "input": {
                    "meetingName": "integ-test (meeting preparation)",
                    "meetingPlatform": meeting.platform,
                    "meetingId": meeting.meeting_id,
                    "meetingPassword": meeting.password or None,
                    "isScheduled": True,
                    "status": "SCHEDULED",
                    "meetingTime": int(time.time()) + SCHEDULE_LEAD_S,
                    "owner": OWNER,
                    "labels": LABELS,
                    "audience": "internal",
                    "meetingContext": CONTEXT,
                    "checklist": CHECKLIST,
                }
            },
        )["createVirtualParticipant"]
        vp_id = created["id"]
        # Edited at once, well before the 5-minute cutoff.
        client.appsync.graphql(
            EDIT_VP,
            {"input": {"id": vp_id, "audience": "external", "meetingContext": "Edited context",
                       "checklist": [CHECKLIST[0]]}},
        )
        row = wait_for(
            lambda: (r := live_meeting.read_vp(client.appsync.graphql, vp_id)) and r.get("CallId") and r,
            timeout=LAUNCH_AND_JOIN_TIMEOUT_S,
            interval=10.0,
        )
        assert row, meeting.redact(f"Virtual Participant {vp_id} did not create its meeting in time")
        record = wait_for(
            lambda: live_meeting.read_meeting(aws["ddb"], aws["table"], row["CallId"]),
            timeout=MEETING_RECORD_TIMEOUT_S,
            interval=3.0,
        )
        assert record, "the meeting record was not created"
        assert _labels(record) == LABELS
        assert record["Audience"] == "external"
        assert record["MeetingContext"] == "Edited context"
        assert [(item["ItemId"], item["Status"]) for item in record["Checklist"]] == [("q1", "open")]
    finally:
        _end(client, vp_id, "integ-test meeting preparation: ended after the check")
