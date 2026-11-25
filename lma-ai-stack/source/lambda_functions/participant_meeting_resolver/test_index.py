#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""The Participant's read-only view: who is answered, and with which fields.

No AWS: the table is a fake and the S3 client only signs links, which it does
offline with the placeholder credentials set here.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ["EVENT_SOURCING_TABLE_NAME"] = "lma-call-events"
os.environ["RECORDINGS_BUCKET_NAME"] = "lma-recordings"

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "lambda_layers" / "configuration_layer"))
sys.path.insert(0, str(HERE))

_SPEC = importlib.util.spec_from_file_location("participant_meeting_index", HERE / "index.py")
index = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(index)
import participant_view  # noqa: E402

ME = "pat@example.com"
OTHER = "olga@example.com"
OWNER = "owner@example.com"
AUDIO = "https://lma-recordings.s3.us-east-1.amazonaws.com/lma-audio-recordings/m1.wav"
VIDEO = "https://lma-recordings.s3.us-east-1.amazonaws.com/lma-video-recordings/m1%20one.mp4"

#: Every attribute a meeting record can hold that a Participant must not be given.
OWNER_ONLY = {
    "MeetingContext": "Pricing floor is 40k; do not mention the merger.",
    "MeetingContextUpdatedBy": OWNER,
    "MeetingContextUpdatedAt": "2026-11-24T10:00:00Z",
    "Checklist": [
        {"ItemId": "c1", "Text": "Ask about budget", "Status": "covered", "Evidence": []}
    ],
    "ChecklistUpdatedAt": "2026-11-24T10:00:00Z",
    "Labels": [{"Key": "engagement", "Value": "acme-renewal"}],
    "Audience": "external",
    "SummaryProfile": "discovery-session",
    "GuidanceEnabled": True,
    "WhiteboardsEnabled": True,
    "SharedWith": [ME, OTHER],
    "AutoSharedWith": [ME],
    "Sentiment": {"OverallSentiment": {"REMOTE": 2}},
    "Metadatajson": "{}",
    "AgentId": OWNER,
    "RecordingUrl": AUDIO,
    "AgendaSource": "ui",
    "AgendaUpdatedBy": OWNER,
    "CaptureSource": "VIRTUAL_PARTICIPANT",
    "TranscriptionEngine": "TRANSCRIBE",
    "ParticipantsUpdatedBy": OWNER,
}


def _meeting(**overrides):
    item = {
        "PK": "c#m1",
        "SK": "c#m1",
        "CallId": "m1",
        "CreatedAt": "2026-11-24T10:00:00.000Z",
        "UpdatedAt": "2026-11-24T11:00:00.000Z",
        "Status": "ENDED",
        "Owner": OWNER,
        "MeetingPlatform": "Zoom",
        "Agenda": "Intro\nBudget",
        "CallSummaryText": "We agreed the plan.",
        "CallSummaryState": json.dumps({"source": "generated", "status": "idle", "profile": "x"}),
        "ActionItems": [{"Text": "Send deck", "Owner": "Olga", "Internal": "x"}],
        "SpeakerIdentities": json.dumps(
            {
                "identities": {
                    "REMOTE#spk_0": {"name": "Olga", "status": "confirmed", "source": "x"}
                },
                "merges": {},
                "segmentOverrides": {},
                "candidates": ["Olga", "Secret Person"],
                "rejected": {"REMOTE#spk_1": ["Wrong"]},
                "lastPass": {"at": "x"},
            }
        ),
        "CustomerPhoneNumber": "Others",
        "Participants": [{"name": "Pat", "email": ME}, {"name": "Olga", "email": OTHER}],
        "Consent": {
            "Mode": "opt_in",
            "Status": "granted",
            "CaptureAllowed": True,
            "NoticeText": "This meeting is recorded.",
            "Events": [
                {
                    "ParticipantName": "Olga",
                    "Decision": "consent",
                    "Source": "chat",
                    "RecordedBy": "LMA",
                }
            ],
            "Attestation": {"Statement": "owner says so"},
            "Evidence": [{"S3Key": "lma-consent-evidence/m1/a.png"}],
            "StatusHistory": [{"Status": "granted"}],
        },
        "RecordingUrls": [AUDIO],
        "RecordingUrlSessions": [1],
        "VideoRecordingUrl": VIDEO,
        **OWNER_ONLY,
    }
    item.update(overrides)
    return item


class FakeTable:
    def __init__(self, meetings=(), segments=(), list_rows=()):
        self.meetings = {m["PK"]: m for m in meetings}
        self.segments = list(segments)
        self.list_rows = list(list_rows)
        self.queries = []

    def get_item(self, Key, ConsistentRead=False):  # noqa: N803
        item = self.meetings.get(Key["PK"])
        return {"Item": item} if item else {}

    def query(self, **kwargs):
        self.queries.append(kwargs)
        if kwargs.get("IndexName"):
            return {"Items": list(self.list_rows)}
        return {"Items": list(self.segments), "LastEvaluatedKey": {"PK": "trs#m1", "SK": "s9"}}


@pytest.fixture
def table(monkeypatch):
    fake = FakeTable([_meeting()])

    class Resource:
        def Table(self, _name):  # noqa: N802
            return fake

    monkeypatch.setattr(index, "DYNAMODB", Resource())
    return fake


def _event(field, username=ME, groups=("Participant",), **arguments):
    return {
        "info": {"fieldName": field},
        "identity": {"username": username, "groups": list(groups)},
        "arguments": arguments,
    }


# ── who is answered ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "field",
    [
        "getParticipantMeeting",
        "getParticipantMeetingTranscript",
        "getParticipantMeetingRecordings",
    ],
)
def test_a_meeting_not_shared_with_the_participant_is_refused(table, field) -> None:
    with pytest.raises(PermissionError, match="Unauthorized"):
        index.handler(_event(field, username="stranger@example.com", CallId="m1"), None)


@pytest.mark.parametrize("field", ["getParticipantMeeting", "getParticipantMeetingTranscript"])
def test_a_meeting_that_does_not_exist_is_refused_in_the_same_words(table, field) -> None:
    with pytest.raises(PermissionError, match="is not authorized to read this meeting"):
        index.handler(_event(field, CallId="no-such-meeting"), None)


def test_owning_a_meeting_is_not_enough_a_participant_reads_shared_meetings_only(table) -> None:
    table.meetings["c#m2"] = _meeting(
        PK="c#m2", SK="c#m2", CallId="m2", Owner=ME, SharedWith=[OTHER]
    )
    with pytest.raises(PermissionError):
        index.handler(_event("getParticipantMeeting", CallId="m2"), None)


@pytest.mark.parametrize("groups", [(), ("User",), ("Admin", "Participant")])
def test_only_the_participant_role_is_answered(table, groups) -> None:
    with pytest.raises(PermissionError):
        index.handler(_event("getParticipantMeeting", groups=groups, CallId="m1"), None)


def test_shared_with_is_compared_entry_by_entry(table) -> None:
    table.meetings["c#m1"] = _meeting(SharedWith=f"[x{ME}, {OTHER}]")
    with pytest.raises(PermissionError):
        index.handler(_event("getParticipantMeeting", CallId="m1"), None)
    table.meetings["c#m1"] = _meeting(SharedWith=f"[{OTHER}, {ME}]")
    assert index.handler(_event("getParticipantMeeting", CallId="m1"), None)["CallId"] == "m1"


# ── what they are given ─────────────────────────────────────────────────────


def test_the_meeting_holds_only_the_fields_of_the_participant_view(table) -> None:
    meeting = index.handler(_event("getParticipantMeeting", CallId="m1"), None)
    assert set(meeting) <= set(participant_view.MEETING_FIELDS)
    for field in OWNER_ONLY:
        assert field not in meeting, field
    text = json.dumps(meeting)
    for secret in (
        "Pricing floor",
        "acme-renewal",
        OTHER,
        "Secret Person",
        "Wrong",
        "owner says so",
        "a.png",
    ):
        assert secret not in text, secret
    assert meeting["Agenda"] == "Intro\nBudget"
    assert meeting["CallSummaryText"] == "We agreed the plan."
    assert meeting["SummaryStatus"] == "idle"
    assert meeting["ActionItems"] == [{"Text": "Send deck", "Owner": "Olga"}]
    assert meeting["ParticipantNames"] == ["Pat", "Olga"]
    assert meeting["Owner"] == OWNER
    assert meeting["HasAudioRecording"] is True and meeting["HasVideoRecording"] is True


def test_the_speaker_names_keep_the_name_map_and_nothing_else(table) -> None:
    names = json.loads(
        index.handler(_event("getParticipantMeeting", CallId="m1"), None)["SpeakerIdentities"]
    )
    assert names == {
        "version": 1,
        "identities": {"REMOTE#spk_0": {"name": "Olga", "status": "confirmed"}},
        "merges": {},
        "segmentOverrides": {},
        "speakerModes": {},
    }


def test_the_consent_record_is_its_status_notice_and_replies(table) -> None:
    consent = index.handler(_event("getParticipantMeeting", CallId="m1"), None)["Consent"]
    assert consent == {
        "Mode": "opt_in",
        "Status": "granted",
        "CaptureAllowed": True,
        "NoticeText": "This meeting is recorded.",
        "Replies": [{"ParticipantName": "Olga", "Decision": "consent", "Source": "chat"}],
    }


def test_the_transcript_is_speech_and_meeting_chat_only_and_each_line_is_allowlisted(table) -> None:
    table.segments = [
        {
            "PK": "trs#m1",
            "SK": "s1",
            "CallId": "m1",
            "SegmentId": "s1",
            "Channel": "REMOTE",
            "Transcript": "Hello",
            "Speaker": "spk_0",
            "StartTime": 1,
            "EndTime": 2,
            "IsPartial": False,
            "Owner": OWNER,
            "SharedWith": f"[{ME}, {OTHER}]",
            "Sentiment": "POSITIVE",
        }
    ]
    page = index.handler(_event("getParticipantMeetingTranscript", CallId="m1"), None)
    (segment,) = page["Segments"]
    assert set(segment) <= set(participant_view.TRANSCRIPT_SEGMENT_FIELDS)
    assert "SharedWith" not in segment and "Owner" not in segment and "Sentiment" not in segment
    (query,) = table.queries
    (channels,) = (
        query["FilterExpression"].get_expression()["values"][1].get_expression()["values"][1:]
    )
    assert sorted(channels) == sorted(["REMOTE", "LOCAL", "CALLER", "AGENT", "MEETING_CHAT"])
    assert "CHAT_ASSISTANT" not in channels and "AGENT_ASSISTANT" not in channels
    # The next page resumes this meeting's transcript, and no other.
    token = page["nextToken"]
    index.handler(_event("getParticipantMeetingTranscript", CallId="m1", nextToken=token), None)
    assert table.queries[-1]["ExclusiveStartKey"] == {"PK": "trs#m1", "SK": "s9"}
    table.meetings["c#m2"] = _meeting(PK="c#m2", SK="c#m2", CallId="m2")
    with pytest.raises(PermissionError, match="invalid nextToken"):
        index.handler(_event("getParticipantMeetingTranscript", CallId="m2", nextToken=token), None)


def test_recordings_are_signed_links_to_the_two_recording_prefixes_only(table) -> None:
    table.meetings["c#m1"] = _meeting(
        RecordingUrls=[
            AUDIO,
            "https://lma-recordings.s3.us-east-1.amazonaws.com/lma-transcripts/m1.txt",
        ],
        RecordingUrlSessions=[1, 2],
    )
    links = index.handler(_event("getParticipantMeetingRecordings", CallId="m1"), None)
    assert [(link["Kind"], link["SessionNumber"]) for link in links] == [
        ("AUDIO", 1),
        ("VIDEO", None),
    ]
    audio = urlparse(links[0]["Url"])
    assert audio.hostname.startswith("lma-recordings.s3")
    assert audio.path == "/lma-audio-recordings/m1.wav"
    query = parse_qs(audio.query)
    assert query["X-Amz-Expires"] == ["3600"]
    assert urlparse(links[1]["Url"]).path == "/lma-video-recordings/m1%20one.mp4"


def test_a_link_to_another_bucket_is_not_signed(table) -> None:
    table.meetings["c#m1"] = _meeting(
        RecordingUrls=[
            "https://someone-else.s3.us-east-1.amazonaws.com/lma-audio-recordings/m1.wav"
        ],
        RecordingUrl=None,
        VideoRecordingUrl=None,
    )
    assert index.handler(_event("getParticipantMeetingRecordings", CallId="m1"), None) == []


def test_no_recording_is_offered_once_the_recordings_were_deleted(table) -> None:
    table.meetings["c#m1"] = _meeting(RecordingsDeletedAt="2026-11-24T12:00:00Z")
    assert index.handler(_event("getParticipantMeetingRecordings", CallId="m1"), None) == []
    meeting = index.handler(_event("getParticipantMeeting", CallId="m1"), None)
    assert meeting["HasAudioRecording"] is False


# ── the list ────────────────────────────────────────────────────────────────


def test_the_list_holds_the_meetings_shared_with_the_participant_and_only_their_summary(
    table,
) -> None:
    table.meetings["c#m2"] = _meeting(PK="c#m2", SK="c#m2", CallId="m2", SharedWith=[OTHER])
    table.list_rows = [
        {
            "ItemType": "call",
            "PK": "cls#a",
            "SK": "ts#2",
            "CallId": "m1",
            "SharedWith": [ME, OTHER],
        },
        {"ItemType": "call", "PK": "cls#a", "SK": "ts#1", "CallId": "m2", "SharedWith": f"[x{ME}]"},
    ]
    result = index.handler(_event("listParticipantMeetings"), None)
    assert [m["CallId"] for m in result["Meetings"]] == ["m1"]
    assert set(result["Meetings"][0]) <= set(participant_view.MEETING_SUMMARY_FIELDS)
    assert result["nextToken"] is None
    (query,) = table.queries
    assert query["IndexName"] == "TypeDateIndex" and query["ScanIndexForward"] is False


def test_the_list_resumes_after_the_last_row_it_returned(table) -> None:
    table.meetings["c#m2"] = _meeting(PK="c#m2", SK="c#m2", CallId="m2")
    table.list_rows = [
        {"ItemType": "call", "PK": "cls#a", "SK": "ts#2", "CallId": "m1", "SharedWith": [ME]},
        {"ItemType": "call", "PK": "cls#a", "SK": "ts#1", "CallId": "m2", "SharedWith": [ME]},
    ]
    result = index.handler(_event("listParticipantMeetings", limit=1), None)
    assert [m["CallId"] for m in result["Meetings"]] == ["m1"]
    assert json.loads(__import__("base64").urlsafe_b64decode(result["nextToken"])) == {
        "ItemType": "call",
        "PK": "cls#a",
        "SK": "ts#2",
    }


# ── the allowlist, the schema and the documentation agree ───────────────────

REPO = HERE.parents[3]
SCHEMA = (REPO / "lma-ai-stack" / "source" / "appsync" / "schema.graphql").read_text()
DOC = (REPO / "docs" / "user-based-access-control.md").read_text()


def _type_fields(name):
    import re

    body = SCHEMA[SCHEMA.index(f"type {name} ") :]
    body = body[body.index("{") + 1 : body.index("\n}")]
    return {m.group(1) for m in re.finditer(r"^\s*(\w+)\s*:", body, re.M)}


@pytest.mark.parametrize(
    "type_name,fields",
    [
        ("ParticipantMeeting", participant_view.MEETING_FIELDS),
        ("ParticipantTranscriptSegment", participant_view.TRANSCRIPT_SEGMENT_FIELDS),
        ("ParticipantConsentReply", participant_view.CONSENT_REPLY_FIELDS),
    ],
)
def test_each_participant_type_declares_exactly_the_fields_the_view_builds(
    type_name, fields
) -> None:
    assert _type_fields(type_name) == set(fields)


def test_the_consent_type_declares_the_consent_fields_and_the_replies() -> None:
    assert _type_fields("ParticipantMeetingConsent") == set(participant_view.CONSENT_FIELDS) | {
        "Replies"
    }


def test_the_documentation_names_every_field_the_view_builds_beyond_the_meeting_basics() -> None:
    section = DOC[DOC.index("### What a Participant sees of a shared meeting") :]
    section = section[: section.index("### The meeting assistant for a Participant")]
    named = set(participant_view.MEETING_FIELDS) - set(participant_view.MEETING_SUMMARY_FIELDS)
    for field in sorted(named | {"CallId", "Owner"}):
        assert f"`{field}`" in section, field
    for owner_only in ("meeting context", "checklist", "labels", "sharing list", "whiteboards"):
        assert owner_only in section, owner_only


def test_the_owner_only_attributes_are_in_no_participant_type() -> None:
    declared = set()
    for name in (
        "ParticipantMeeting",
        "ParticipantTranscriptSegment",
        "ParticipantMeetingConsent",
        "ParticipantConsentReply",
        "ParticipantMeetingRecording",
    ):
        declared |= _type_fields(name)
    assert not declared & set(OWNER_ONLY), sorted(declared & set(OWNER_ONLY))
