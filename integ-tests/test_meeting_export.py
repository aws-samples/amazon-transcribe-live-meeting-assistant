# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Summary profiles, structured action items and the meeting export, end to end.

See docs/transcript-summarization.md#summary-profiles and docs/meeting-export.md
for the behaviour asserted.

Default run (about ten minutes, about a dozen model calls, self-cleaning). It
needs the Cognito test user (cognito_test_user.py) and a stack with the MCP API
key endpoint; it skips without either, and when the test user already holds an
MCP API key (one key per user, and the test cannot tell whether someone relies on
it).

``test_a_discovery_session_meeting_is_summarised_with_its_profile``
    Puts on the call data stream a START naming the ``discovery-session``
    profile and a checklist, writes a short discovery conversation and a chat
    message, and ends the meeting. Waits for the end-of-meeting summary and
    checks it was written with that profile's sections, with structured action
    items whose owners are names said in the meeting.
``test_the_export_over_mcp_matches_the_published_schema``
    ``get_meeting_export`` on that meeting validates against
    docs/schemas/meeting-export-v1.schema.json, starts with its ``format``, and
    holds its participants, links, summary and action items. Checklist evidence
    and action items carry their segment's start and end times; with a consent
    reply recorded under a participant's exact name, every participant's
    ``consent`` is still ``unknown``, as a name is never matched to a speaker.
    With ``LMA_TEST_MEETING_EXPORT_OUT`` set, the export is also written to that
    file.
``test_the_profile_is_set_on_the_meeting_and_chosen_for_a_regeneration``
    ``list_summary_profiles``; ``update_meeting_metadata`` sets and refuses a
    profile; ``regenerate_meeting_summary`` with ``healthcare`` writes that
    profile's summary, keeps the meeting's own profile and clears the action items.
``test_the_export_download_is_one_json_file``
    ``prepareMeetingArchive(Contents: EXPORT)`` as the signed-in user, read back
    through ``getMeetingArchive`` and downloaded through its link.
``test_a_large_export_is_answered_with_a_link_for_ten_minutes``
    A second meeting with more than 6 MB of transcript: the tool answers with a
    link, signed for at most ten minutes, to an export that validates.
``test_an_export_after_the_transcript_was_deleted_leaves_it_out``
    Runs the transcript deletion function on the first meeting, as a scheduled
    deletion does, and exports again.
``test_a_spoken_consent_is_given_as_its_speaker_s_through_the_live_pipeline``
    A third meeting: two lines of speech put on the call data stream, then,
    as the backend role a Virtual Participant signs as (an assumed role; the
    test skips otherwise), a notify consent with a spoken reply linked to its
    line, one whose line is somebody else's, and a chat reply under the other
    voice's name. ``get_meeting_consent`` and the export give the linked
    speaker's ``speakerId`` and ``granted``, and nothing for the others; a
    rename keeps it; after the transcript deletion function runs the link is
    kept and names no speaker.
``test_a_meeting_the_user_cannot_see_is_refused``
    A meeting owned by somebody else (skipped when the test user is an Admin).
"""

from __future__ import annotations

import contextlib
import json
import os
import time
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import cognito_test_user
import jsonschema
import pytest

SCHEMA = json.loads(
    (Path(__file__).resolve().parents[1] / "docs" / "schemas" / "meeting-export-v1.schema.json")
    .read_text(encoding="utf-8")
)
VALIDATOR = jsonschema.Draft202012Validator(SCHEMA)

SUMMARY_TIMEOUT_S = 15 * 60
REGENERATE_TIMEOUT_S = 10 * 60
ARCHIVE_TIMEOUT_S = 5 * 60

PREPARE = """
mutation Prepare($CallId: ID!, $Contents: MeetingArchiveContents) {
  prepareMeetingArchive(CallId: $CallId, Contents: $Contents) { ArchiveId Status FileName }
}
"""
READ = """
query Read($CallId: ID!, $ArchiveId: ID!) {
  getMeetingArchive(CallId: $CallId, ArchiveId: $ArchiveId) { Status FileName Size DownloadUrl Message }
}
"""

#: A short discovery conversation: a self-introduction, a requirement, a
#: decision, a constraint, an action item with an owner and a date, an open
#: question, and an action item nobody took.
CONVERSATION = [
    ("s1", 0, 6, "Priya Shah", "Thanks for joining. I lead the claims platform team."),
    ("s2", 7, 14, "Sam Lee", "We need single sign-on with our existing identity provider. That is a must for the pilot."),
    ("s3", 15, 22, "Priya Shah", "Agreed. We decided to start the pilot with the Boston office in January."),
    ("s4", 23, 30, "Sam Lee", "Our budget is capped at fifty thousand dollars this year, so that is a hard constraint."),
    ("s5", 31, 38, "Priya Shah", "I will send the revised proposal by 2026-12-01."),
    ("s6", 39, 46, "Sam Lee", "Who owns the data migration? We still need an answer from the IT team on that."),
    ("s7", 47, 52, "Priya Shah", "Someone should book a follow-up workshop after the pilot starts."),
]
CHAT = ("c1", 53, "Sam Lee", "Our requirements: https://docs.example.com/claims-requirements")
CHECKLIST = [
    {"ItemId": "q1", "Text": "Who owns the data migration?", "Kind": "question", "Priority": "must",
     "Status": "open", "Evidence": [], "UpdatedBy": "LMA", "UpdatedAt": "2026-11-20T00:00:00Z"},
    {"ItemId": "r1", "Text": "Budget limits", "Kind": "risk", "Priority": "should",
     "Status": "open", "Evidence": [], "UpdatedBy": "LMA", "UpdatedAt": "2026-11-20T00:00:00Z"},
]
DISCOVERY_SECTIONS = {
    "SUMMARY",
    "DECISIONS",
    "ACTION ITEMS",
    "OPEN QUESTIONS",
    "RISKS AND CONSTRAINTS",
    "REQUIREMENTS HEARD",
    "CHECKLIST COVERAGE",
}


def valid(document: dict) -> None:
    errors = sorted(VALIDATOR.iter_errors(document), key=lambda e: list(e.path))
    assert not errors, "\n".join(f"{list(e.path)}: {e.message}" for e in errors[:20])


def _wait(predicate, timeout: float, interval: float = 10.0):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(interval)
    return last


class Meeting:
    """A synthetic meeting of the test user's, and everything written for it."""

    def __init__(self, env: "Environment", owner: str, label: str):
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H-%M-%S")
        self.env = env
        self.owner = owner
        self.call_id = f"LMA integ export {label} {uuid.uuid4().hex[:6]}-{stamp}"
        self.created_at = None

    def start(self, **fields: Any) -> None:
        record = {
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
            **fields,
        }
        self.env.kinesis.put_record(
            StreamName=self.env.stream, PartitionKey=self.call_id, Data=json.dumps(record).encode()
        )
        row = _wait(self.row, 120, 3)
        assert row, "the synthetic meeting was not created (see test_kds_pipeline_creates_meeting)"
        self.created_at = row["CreatedAt"]["S"]

    def end(self) -> None:
        self.env.kinesis.put_record(
            StreamName=self.env.stream,
            PartitionKey=self.call_id,
            Data=json.dumps(
                {"EventType": "END", "CallId": self.call_id, "UpdatedAt": datetime.now(timezone.utc).isoformat()}
            ).encode(),
        )

    def row(self) -> dict | None:
        key = {"PK": {"S": f"c#{self.call_id}"}, "SK": {"S": f"c#{self.call_id}"}}
        return self.env.ddb.get_item(TableName=self.env.table, Key=key, ConsistentRead=True).get("Item")

    def segment(self, segment_id, start, end, speaker, text, channel="REMOTE") -> dict:
        return {
            "PK": {"S": f"trs#{self.call_id}"},
            "SK": {"S": f"ts#{start:09.3f}#{segment_id}"},
            "CallId": {"S": self.call_id},
            "SegmentId": {"S": segment_id},
            "StartTime": {"N": str(start)},
            "EndTime": {"N": str(end)},
            "Transcript": {"S": text},
            "Channel": {"S": channel},
            "Speaker": {"S": speaker},
            "IsPartial": {"BOOL": False},
            "CreatedAt": {"S": datetime.now(timezone.utc).isoformat()},
            "Owner": {"S": self.owner},
            "ExpiresAfter": {"N": str(int(time.time()) + 86400)},
        }

    def put_segments(self, items: list[dict]) -> None:
        for offset in range(0, len(items), 25):
            batch = [{"PutRequest": {"Item": item}} for item in items[offset : offset + 25]]
            pending = {self.env.table: batch}
            while pending:
                pending = self.env.ddb.batch_write_item(RequestItems=pending).get("UnprocessedItems") or {}

    def cleanup(self) -> None:
        ddb, table = self.env.ddb, self.env.table
        for pk in (f"c#{self.call_id}", f"trs#{self.call_id}", f"nt#{self.call_id}", f"kf#{self.call_id}"):
            with contextlib.suppress(Exception):
                for page in ddb.get_paginator("query").paginate(
                    TableName=table,
                    KeyConditionExpression="PK = :pk",
                    ExpressionAttributeValues={":pk": {"S": pk}},
                    ProjectionExpression="PK, SK",
                ):
                    for item in page.get("Items", []):
                        ddb.delete_item(TableName=table, Key={"PK": item["PK"], "SK": item["SK"]})
        created = self.created_at or ""
        if len(created) >= 13:
            shard = int(created[11:13]) // 4
            with contextlib.suppress(Exception):
                ddb.delete_item(
                    TableName=table,
                    Key={
                        "PK": {"S": f"cls#{created[:10]}#s#{shard:02d}"},
                        "SK": {"S": f"ts#{created}#id#{self.call_id}"},
                    },
                )
        posix = "".join(c if c.isalnum() or c in "_." else "_" for c in self.call_id).strip("_")
        with contextlib.suppress(Exception):
            for page in self.env.s3.get_paginator("list_objects_v2").paginate(
                Bucket=self.env.bucket, Prefix=f"lma-meeting-archives/{posix}/"
            ):
                for entry in page.get("Contents", []):
                    self.env.s3.delete_object(Bucket=self.env.bucket, Key=entry["Key"])


class Environment:
    """The stack, the test user, and an MCP API key of theirs for the module."""

    def __init__(self, probe_module, deployment, id_token: str, username: str, admin: bool):
        self.probe_module = probe_module
        self.stack_name = ""
        self.d = deployment
        self.id_token = id_token
        self.username = username
        self.admin = admin
        self.kinesis = deployment.kinesis
        self.ddb = deployment.ddb
        self.s3 = deployment.s3
        self.table = deployment.table
        self.bucket = deployment.bucket
        self.stream = deployment.outputs["CallDataStreamName"]
        self.key = None
        self.prefix = None
        self.meetings: list[Meeting] = []
        self.state: dict[str, Any] = {}

    def meeting(self, label: str, owner: str | None = None) -> Meeting:
        created = Meeting(self, owner or self.username, label)
        self.meetings.append(created)
        return created

    def call(self, name: str, arguments: dict) -> tuple[bool, Any]:
        probe = self.probe_module.Probe(self.d, self.id_token, self.username)
        return probe.call(self.key, name, arguments)

    def graphql(self, query: str, variables: dict) -> dict:
        return self.probe_module._graphql(self.d.graphql_url, self.id_token, query, variables)


@pytest.fixture(scope="module")
def env(stack_name, region):
    credentials = cognito_test_user.resolve(region)
    if credentials is None:
        pytest.skip(f"no Cognito test user — {cognito_test_user.describe_sources()}")
    try:
        import pycognito  # noqa: F401
    except ImportError:
        pytest.skip("pycognito not installed (pip install -r integ-tests/requirements.txt)")
    import mcp_meeting_tools_probe as probe_module

    deployment = probe_module.Deployment(stack_name, region)
    if not deployment.endpoint:
        pytest.skip(f"{stack_name} has no MCP API key endpoint (EnableMCP=false)")
    username, password = credentials
    outputs = deployment.outputs
    pool_id = outputs.get("MCPServerUserPoolId") or outputs["CognitoUserPoolTokenIssuerUrl"].rsplit("/", 1)[-1]
    tokens = probe_module._mint_tokens(
        deployment.session.region_name, pool_id, outputs["CognitoUserPoolClientId"], username, password
    )
    claims = probe_module._claims(tokens["id"])
    owner = claims.get("cognito:username") or username
    if probe_module._graphql(deployment.graphql_url, tokens["id"], probe_module.LIST_KEYS)["listMCPApiKeys"]:
        pytest.skip("the test user already holds an MCP API key, which this test will not replace")
    environment = Environment(
        probe_module, deployment, tokens["id"], owner, "Admin" in (claims.get("cognito:groups") or [])
    )
    environment.stack_name = stack_name
    probe = probe_module.Probe(deployment, tokens["id"], owner)
    environment.key, environment.prefix = probe.generate_key("read-write")
    try:
        yield environment
    finally:
        with contextlib.suppress(Exception):
            probe.revoke(environment.prefix)
        for meeting in environment.meetings:
            meeting.cleanup()


@pytest.fixture(scope="module")
def discovery_meeting(env):
    meeting = env.meeting("discovery")
    meeting.start(SummaryProfile="discovery-session", Checklist=CHECKLIST, ChecklistUpdatedAt="2026-11-20T00:00:00Z")
    rows = [meeting.segment(i, s, e, who, text) for i, s, e, who, text in CONVERSATION]
    chat_id, chat_start, chat_sender, chat_text = CHAT
    rows.append(meeting.segment(chat_id, chat_start, chat_start + 1, chat_sender, chat_text, "MEETING_CHAT"))
    meeting.put_segments(rows)
    # Evidence that names a segment and no time, as a consumer that renumbers
    # segments would need placing by its times.
    env.ddb.update_item(
        TableName=env.table,
        Key={"PK": {"S": f"c#{meeting.call_id}"}, "SK": {"S": f"c#{meeting.call_id}"}},
        UpdateExpression="SET Checklist[0].Evidence = :e",
        ExpressionAttributeValues={
            ":e": {"L": [{"M": {"SegmentId": {"S": "s6"}, "Quote": {"S": "Who owns the data migration?"}}}]}
        },
    )
    # The segments are written to the table directly, so the meeting gets the
    # duration the call event processor's aggregation would have given it.
    env.ddb.update_item(
        TableName=env.table,
        Key={"PK": {"S": f"c#{meeting.call_id}"}, "SK": {"S": f"c#{meeting.call_id}"}},
        UpdateExpression="SET TotalConversationDurationMillis = :d",
        ExpressionAttributeValues={":d": {"N": "54000"}},
    )
    meeting.end()
    return meeting


def _summary_state(meeting: Meeting) -> dict:
    row = meeting.row() or {}
    raw = row.get("CallSummaryState", {}).get("S")
    return json.loads(raw) if raw else {}


def _sections(meeting: Meeting) -> dict:
    row = meeting.row() or {}
    return json.loads(row.get("CallSummaryText", {}).get("S") or "{}")


# ── the end-of-meeting summary ──────────────────────────────────────────────


def test_a_discovery_session_meeting_is_summarised_with_its_profile(env, discovery_meeting) -> None:
    def summarised():
        state = _summary_state(discovery_meeting)
        return state if state.get("status") in ("idle", "failed", "empty") else None

    state = _wait(summarised, SUMMARY_TIMEOUT_S)
    assert state, "no end-of-meeting summary within the timeout"
    assert state["status"] == "idle", state
    assert state.get("profile") == "discovery-session", state
    assert state.get("actionItems") == "generated", state
    sections = set(_sections(discovery_meeting))
    assert DISCOVERY_SECTIONS <= sections, sections
    # No agenda, so no agenda coverage.
    assert "AGENDA COVERAGE" not in sections
    row = discovery_meeting.row()
    assert row["SummaryProfile"]["S"] == "discovery-session"
    items = [{k: list(v.values())[0] for k, v in i["M"].items()} for i in row["ActionItems"]["L"]]
    assert items, "the profile lists structured action items"
    spoken = " ".join(text for *_rest, text in CONVERSATION) + " Priya Shah Sam Lee"
    for item in items:
        owner = item.get("Owner")
        if owner and owner is not True:
            assert owner.casefold() in spoken.casefold(), f"{owner!r} was not named in the meeting"
    env.state["action_items"] = items


#: A consent record with a reply under a participant's exact name, written for
#: the export test and removed after it.
NAMED_REPLY_CONSENT = {
    "M": {
        "Mode": {"S": "opt_in"},
        "Rule": {"S": "each_participant"},
        "Status": {"S": "granted"},
        "CaptureAllowed": {"BOOL": True},
        "Events": {
            "L": [
                {
                    "M": {
                        "ParticipantName": {"S": "Priya Shah"},
                        "Decision": {"S": "consent"},
                        "Source": {"S": "chat"},
                        "OccurredAt": {"S": "2026-11-20T00:00:00Z"},
                    }
                }
            ]
        },
    }
}


def _set_consent(env, meeting: Meeting, consent: dict | None) -> None:
    key = {"PK": {"S": f"c#{meeting.call_id}"}, "SK": {"S": f"c#{meeting.call_id}"}}
    if consent is None:
        env.ddb.update_item(TableName=env.table, Key=key, UpdateExpression="REMOVE Consent")
    else:
        env.ddb.update_item(
            TableName=env.table,
            Key=key,
            UpdateExpression="SET Consent = :c",
            ExpressionAttributeValues={":c": consent},
        )


def test_the_export_over_mcp_matches_the_published_schema(env, discovery_meeting) -> None:
    _set_consent(env, discovery_meeting, NAMED_REPLY_CONSENT)
    try:
        error, document = env.call("get_meeting_export", {"meetingId": discovery_meeting.call_id})
    finally:
        _set_consent(env, discovery_meeting, None)
    assert not error, document
    valid(document)
    assert list(document)[:2] == ["format", "schemaVersion"], list(document)[:3]
    assert document["format"] == "lma-meeting-export"
    out = os.environ.get("LMA_TEST_MEETING_EXPORT_OUT")
    if out:
        Path(out).write_text(json.dumps(document, indent=2), encoding="utf-8")
    assert document["meeting"]["summaryProfile"] == "discovery-session"
    assert document["meeting"]["transcriptRetained"] is True
    assert document["summary"]["profile"] == "discovery-session"
    names = {p["name"]: p for p in document["participants"] if p["name"]}
    assert {"Priya Shah", "Sam Lee"} <= set(names), document["participants"]
    sharer = next(p for p in document["participants"] if p["speakerId"].startswith("chat-"))
    assert document["links"] == [
        {"url": "https://docs.example.com/claims-requirements", "startTime": 53.0, "sharedBy": sharer["speakerId"]}
    ]
    assert {e["kind"] for e in document["transcript"]} == {"speech", "chat"}
    # A reply under "Priya Shah" is not matched to the speaker of that name.
    assert {p["consent"] for p in document["participants"]} == {"unknown"}, document["participants"]
    # The consent part is the record in LMA's own values, as get_meeting_consent gives it.
    assert (document["consent"]["mode"], document["consent"]["rule"]) == ("opt_in", "each_participant")
    assert [(e["participantName"], e["decision"], e["source"], e["speakerId"]) for e in document["consent"]["events"]] == [
        ("Priya Shah", "consent", "chat", None)
    ], document["consent"]["events"]
    assert document["consent"]["hasEvidence"] is False
    assert document["actionItems"], "the export holds the structured action items"
    times = {e["segmentId"]: (e["startTime"], e["endTime"]) for e in document["transcript"]}
    for item in document["actionItems"]:
        assert "endTime" in item, item
        if item["segmentId"] in times:
            assert item["endTime"] == times[item["segmentId"]][1], (item, times[item["segmentId"]])
    assert [c["itemId"] for c in document["checklist"]] == ["q1", "r1"]
    evidence = document["checklist"][0]["evidence"]
    assert [(e["segmentId"], e["startTime"], e["endTime"]) for e in evidence] == [("s6", 39.0, 46.0)], evidence
    error, partial = env.call(
        "get_meeting_export", {"meetingId": discovery_meeting.call_id, "include": ["summary", "actionItems"]}
    )
    assert not error, partial
    valid(partial)
    assert "transcript" not in partial and "participants" not in partial


def test_the_profile_is_set_on_the_meeting_and_chosen_for_a_regeneration(env, discovery_meeting) -> None:
    error, listed = env.call("list_summary_profiles", {})
    assert not error, listed
    names = [p["name"] for p in listed["profiles"]]
    assert names[0] == "default" and {"discovery-session", "healthcare"} <= set(names)

    error, refused = env.call(
        "update_meeting_metadata", {"meetingId": discovery_meeting.call_id, "summaryProfile": "no-such-profile"}
    )
    assert error and "no summary profile" in refused, refused
    error, answer = env.call(
        "update_meeting_metadata", {"meetingId": discovery_meeting.call_id, "summaryProfile": "healthcare"}
    )
    assert not error and answer["summaryProfile"] == "healthcare", answer
    error, answer = env.call(
        "update_meeting_metadata", {"meetingId": discovery_meeting.call_id, "summaryProfile": "discovery-session"}
    )
    assert not error and answer["summaryProfile"] == "discovery-session", answer

    before = _summary_state(discovery_meeting).get("updatedAt")
    error, started = env.call(
        "regenerate_meeting_summary", {"meetingId": discovery_meeting.call_id, "summaryProfile": "healthcare"}
    )
    assert not error and started["started"], started

    def regenerated():
        state = _summary_state(discovery_meeting)
        done = state.get("status") in ("idle", "failed") and state.get("updatedAt") != before
        return state if done else None

    state = _wait(regenerated, REGENERATE_TIMEOUT_S)
    assert state and state["status"] == "idle", state
    assert state.get("profile") == "healthcare", state
    assert {"SOAP", "BIRP"} <= set(_sections(discovery_meeting))
    row = discovery_meeting.row()
    # The meeting's own profile is not changed by the choice, and the action
    # items, which belonged to the previous summary, are cleared.
    assert row["SummaryProfile"]["S"] == "discovery-session"
    assert row["ActionItems"]["L"] == []


def test_the_export_download_is_one_json_file(env, discovery_meeting) -> None:
    started = env.graphql(PREPARE, {"CallId": discovery_meeting.call_id, "Contents": "EXPORT"})
    archive = started["prepareMeetingArchive"]
    assert archive["FileName"].endswith("-export.json")

    def ready():
        status = env.graphql(READ, {"CallId": discovery_meeting.call_id, "ArchiveId": archive["ArchiveId"]})
        status = status["getMeetingArchive"]
        return status if status["Status"] != "PREPARING" else None

    status = _wait(ready, ARCHIVE_TIMEOUT_S, 3)
    assert status and status["Status"] == "READY", status
    with urllib.request.urlopen(status["DownloadUrl"], timeout=60) as response:  # noqa: S310
        document = json.loads(response.read())
    valid(document)
    assert document["meeting"]["meetingId"] == discovery_meeting.call_id
    assert "X-Amz-Expires=600" in status["DownloadUrl"]


def test_a_large_export_is_answered_with_a_link_for_ten_minutes(env) -> None:
    meeting = env.meeting("large")
    meeting.start()
    line = "This is a long line of transcript for the large export test. " * 32
    meeting.put_segments(
        [meeting.segment(f"l{n:05d}", n * 2.0, n * 2.0 + 1.5, "Integ Speaker", line) for n in range(3200)]
    )
    error, answer = env.call("get_meeting_export", {"meetingId": meeting.call_id})
    assert not error, answer
    assert set(answer) == {"exportUrl", "expiresAt", "sizeBytes"}, list(answer)[:10]
    expires = datetime.fromisoformat(answer["expiresAt"].replace("Z", "+00:00"))
    assert expires <= datetime.now(timezone.utc) + timedelta(minutes=10, seconds=30)
    with urllib.request.urlopen(answer["exportUrl"], timeout=120) as response:  # noqa: S310
        body = response.read()
    assert len(body) == answer["sizeBytes"]
    document = json.loads(body)
    valid(document)
    assert len(document["transcript"]) == 3200


def _deletion_function(env) -> str:
    """The transcript deletion function of the stack under test."""
    from kds_pipeline_probe import _stack_resources

    cfn = env.d.session.client("cloudformation")
    ai_stack = next(
        r["PhysicalResourceId"]
        for r in _stack_resources(cfn, env.stack_name)
        if r["LogicalResourceId"] == "AISTACK"
    )
    return next(
        r["PhysicalResourceId"]
        for r in _stack_resources(cfn, ai_stack)
        if r["LogicalResourceId"] == "TranscriptDeletionFunction"
    )


def test_an_export_after_the_transcript_was_deleted_leaves_it_out(env, discovery_meeting) -> None:
    response = env.d.session.client("lambda").invoke(
        FunctionName=_deletion_function(env),
        Payload=json.dumps({"CallId": discovery_meeting.call_id, "Trigger": "scheduled"}).encode(),
    )
    assert "FunctionError" not in response, response["Payload"].read()
    error, document = env.call("get_meeting_export", {"meetingId": discovery_meeting.call_id})
    assert not error, document
    valid(document)
    assert document["meeting"]["transcriptRetained"] is False
    assert "transcript" not in document and "participants" not in document and "links" not in document
    assert all(e["quote"] is None for item in document["checklist"] for e in item["evidence"])
    # The segments went with the transcript, so the evidence's end is no longer known.
    assert all(e["endTime"] is None for item in document["checklist"] for e in item["evidence"])
    assert document["summary"]["sections"], "the summary is kept"


#: The consent mutations a Virtual Participant makes, as the backend role it signs with.
START_CONSENT = """
mutation Start($input: StartMeetingConsentInput!) { startMeetingConsent(input: $input) { CallId } }
"""
RECORD_REPLY = """
mutation Reply($input: RecordMeetingConsentEventInput!) { recordMeetingConsentEvent(input: $input) { CallId } }
"""
READ_CONSENT = """
query Read($CallId: ID!) { getMeetingConsent(CallId: $CallId) { Consent {
  Events { ParticipantName Decision Source SpeakerLink { Basis SegmentId Channel Speaker LinkedAt LinkedBy } }
} } }
"""
RENAME = """
mutation Rename($input: UpdateSpeakerIdentitiesInput!) { updateSpeakerIdentities(input: $input) { CallId } }
"""
VOICE = "Integ Voice (spk_0)"
OTHER_VOICE = "Integ Other (spk_1)"


def _spoken_line(meeting: Meeting, segment_id: str, start: float, speaker: str, text: str) -> None:
    """A final line of speech on the call data stream, as kinesis-stream.ts writes one."""
    now = datetime.now(timezone.utc).isoformat()
    record = {
        "EventType": "ADD_TRANSCRIPT_SEGMENT",
        "CallId": meeting.call_id,
        "Channel": "REMOTE",
        "SegmentId": segment_id,
        "StartTime": start,
        "EndTime": start + 2,
        "Transcript": text,
        "IsPartial": False,
        "CreatedAt": now,
        "UpdatedAt": now,
        "Sentiment": None,
        "TranscriptEvent": None,
        "Speaker": speaker,
        "AccessToken": "",
        "IdToken": "",
        "RefreshToken": "",
    }
    meeting.env.kinesis.put_record(
        StreamName=meeting.env.stream, PartitionKey=meeting.call_id, Data=json.dumps(record).encode()
    )


def _stored_lines(meeting: Meeting) -> dict:
    """The meeting's stored lines by segment id: (channel, speaker as stored)."""
    pages = meeting.env.ddb.get_paginator("query").paginate(
        TableName=meeting.env.table,
        KeyConditionExpression="PK = :pk",
        ExpressionAttributeValues={":pk": {"S": f"trs#{meeting.call_id}"}},
        ConsistentRead=True,
    )
    return {
        item["SegmentId"]["S"]: (item["Channel"]["S"], item["Speaker"]["S"])
        for page in pages
        for item in page.get("Items", [])
        if "SegmentId" in item
    }


def test_a_spoken_consent_is_given_as_its_speaker_s_through_the_live_pipeline(env, client) -> None:
    """A reply linked to its transcript line, as a Virtual Participant records one, end to end.

    The lines go through the call data stream and the call event processor, so
    the link is checked against what the stack actually stores; the reply goes
    through AppSync's live schema and the consent resolver as the backend role.
    """
    arn = client.session.client("sts").get_caller_identity().get("Arn", "")
    if ":assumed-role/" not in arn:
        pytest.skip("needs an assumed role, which AppSync treats as the backend a Virtual Participant signs as")
    meeting = env.meeting("speaker link")
    meeting.start()
    _spoken_line(meeting, "vp-REMOTE-r1-g0-s1", 1.0, VOICE, "LMA, I consent.")
    _spoken_line(meeting, "vp-REMOTE-r1-g0-s2", 4.0, OTHER_VOICE, "Good morning, everyone.")
    lines = _wait(lambda: (lambda found: found if len(found) >= 2 else None)(_stored_lines(meeting)), 120, 3)
    assert lines and len(lines) == 2, f"the synthetic lines were not stored: {lines}"
    channel, stored = lines["vp-REMOTE-r1-g0-s1"]
    assert (channel, stored) == ("REMOTE", VOICE), lines

    graphql = client.appsync.graphql
    graphql(START_CONSENT, {"input": {"CallId": meeting.call_id, "Mode": "notify"}})
    line = {"SegmentId": "vp-REMOTE-r1-g0-s1", "Channel": channel, "Speaker": stored}
    reply = {"CallId": meeting.call_id, "Decision": "consent", "Source": "voice"}
    graphql(RECORD_REPLY, {"input": {**reply, "ParticipantName": "Integ Voice", "SpeakerLink": line}})
    # Under another participant's name the line is not theirs, so the reply is stored unlinked.
    graphql(RECORD_REPLY, {"input": {**reply, "ParticipantName": "Integ Third", "SpeakerLink": line}})
    # A chat reply under the other voice's exact name is not matched to it.
    graphql(RECORD_REPLY, {"input": {**reply, "ParticipantName": "Integ Other", "Source": "chat"}})

    stored_events = graphql(READ_CONSENT, {"CallId": meeting.call_id})["getMeetingConsent"]["Consent"]["Events"]
    links = [e["SpeakerLink"] for e in stored_events]
    assert links[0]["Basis"] == "same_segment" and links[0]["LinkedBy"] == "LMA", links
    assert (links[0]["SegmentId"], links[0]["Channel"], links[0]["Speaker"]) == (
        "vp-REMOTE-r1-g0-s1",
        "REMOTE",
        VOICE,
    )
    assert links[1:] == [None, None], links

    error, answer = env.call("get_meeting_consent", {"meetingId": meeting.call_id})
    assert not error, answer
    given = [(e["participantName"], e["speakerId"], e["speakerLinkBasis"]) for e in answer["events"]]
    assert given == [
        ("Integ Voice", "remote-spk_0", "same_segment"),
        ("Integ Third", None, None),
        ("Integ Other", None, None),
    ], answer["events"]
    assert answer["events"][0]["speakerLinkedAt"], answer["events"]

    error, document = env.call("get_meeting_export", {"meetingId": meeting.call_id})
    assert not error, document
    valid(document)
    people = {p["speakerId"]: p for p in document["participants"]}
    assert people["remote-spk_0"]["consent"] == "granted", document["participants"]
    assert people["remote-spk_1"]["consent"] == "unknown", document["participants"]
    # One shape for both, with the same speaker ids.
    assert document["consent"]["events"] == answer["events"]

    # Renaming the voice on the meeting page keeps its consent and its id.
    identities = {
        "identities": {f"REMOTE#{VOICE}": {"name": "Integ Renamed", "status": "confirmed", "source": "manual"}}
    }
    env.graphql(RENAME, {"input": {"CallId": meeting.call_id, "SpeakerIdentities": json.dumps(identities)}})
    error, renamed = env.call("get_meeting_export", {"meetingId": meeting.call_id})
    assert not error, renamed
    voice = next(p for p in renamed["participants"] if p["speakerId"] == "remote-spk_0")
    assert (voice["name"], voice["consent"]) == ("Integ Renamed", "granted"), voice

    # Deleting the transcript keeps the link on the consent record; it no longer names a speaker.
    response = env.d.session.client("lambda").invoke(
        FunctionName=_deletion_function(env),
        Payload=json.dumps({"CallId": meeting.call_id, "Trigger": "scheduled"}).encode(),
    )
    assert "FunctionError" not in response, response["Payload"].read()
    error, after = env.call("get_meeting_consent", {"meetingId": meeting.call_id})
    assert not error, after
    assert [(e["speakerId"], e["speakerLinkBasis"]) for e in after["events"]] == [
        (None, "same_segment"),
        (None, None),
        (None, None),
    ], after["events"]
    kept = graphql(READ_CONSENT, {"CallId": meeting.call_id})["getMeetingConsent"]["Consent"]["Events"]
    assert kept[0]["SpeakerLink"]["SegmentId"] == "vp-REMOTE-r1-g0-s1", kept


def test_a_meeting_the_user_cannot_see_is_refused(env) -> None:
    if env.admin:
        pytest.skip("the test user is an Admin, who may see every meeting")
    meeting = env.meeting("other", owner="someone-else@example.com")
    meeting.start()
    error, answer = env.call("get_meeting_export", {"meetingId": meeting.call_id})
    assert error and "not found, or you do not have access" in answer, answer
