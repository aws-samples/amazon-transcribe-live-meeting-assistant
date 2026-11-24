# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""get_meeting_consent and share_meeting."""

import json
from types import SimpleNamespace

import pytest
from lma_speakers.participants import SPEAKER_FIELDS  # configuration layer

from tools import consent, meeting_actions
from tools._meeting_fakes import ADMIN, OTHER, OWNER, SHARED, caller, meeting_row

RECORD = {
    "CallId": "m1",
    "CaptureAllowed": True,
    "Consent": {
        "Mode": "opt_in",
        "Rule": "each_participant",
        "Status": "granted",
        "CaptureAllowed": True,
        "NoticePostedAt": "2026-11-19T14:00:00Z",
        "Notices": [
            {"Channel": "chat", "PostedAt": "2026-11-19T14:00:00Z"},
            {"Channel": "voice", "PostedAt": "2026-11-19T14:00:05Z"},
        ],
        "Events": [
            {
                "ParticipantName": "ana silva",
                "Decision": "objection",
                "Source": "chat",
                "OccurredAt": "2026-11-19T14:00:58Z",
                "RecordedAt": "2026-11-19T14:01:00Z",
            },
            {
                "ParticipantName": "Ana Silva",
                "Decision": "consent",
                "Source": "voice",
                "RecordedAt": "2026-11-19T14:02:00Z",
            },
            {
                "ParticipantName": "Bo Chen",
                "Decision": "consent",
                "Source": "ui",
                "RecordedAt": "2026-11-19T14:02:30Z",
            },
            {"ParticipantName": "Owner", "Decision": "consent", "Source": "attestation"},
        ],
        "Attestation": None,
        "Evidence": [{"EvidenceId": "e1"}, {"EvidenceId": "e2"}],
    },
}


@pytest.fixture
def meeting(calls_table, appsync):
    calls_table.put(meeting_row())
    appsync.answers["getMeetingConsent"] = {"getMeetingConsent": RECORD}
    return appsync


@pytest.mark.parametrize("who", [caller(OWNER), caller(SHARED), caller(ADMIN, admin=True)])
def test_the_consent_record_is_returned_without_its_images(meeting, who):
    answer = consent.execute(who, "m1")
    query = meeting.calls[0][0]
    assert "SignedUrl" not in query and "S3Key" not in query
    assert answer["hasEvidence"] is True
    assert "Evidence" not in str(answer["events"])
    assert "evidence" not in answer


def test_the_consent_values_are_lma_s_own_and_unchanged(meeting):
    answer = consent.execute(caller(), "m1")
    assert answer == {
        "meetingId": "m1",
        "mode": "opt_in",
        "rule": "each_participant",
        "status": "granted",
        "captureAllowed": True,
        "noticePostedAt": "2026-11-19T14:00:00Z",
        "notices": [
            {"channel": "chat", "postedAt": "2026-11-19T14:00:00Z"},
            {"channel": "voice", "postedAt": "2026-11-19T14:00:05Z"},
        ],
        "events": [
            {
                "participantName": "ana silva",
                "decision": "objection",
                "source": "chat",
                "at": "2026-11-19T14:00:58Z",
                "speakerId": None,
                "speakerLinkBasis": None,
                "speakerLinkedAt": None,
            },
            {
                "participantName": "Ana Silva",
                "decision": "consent",
                "source": "voice",
                "at": "2026-11-19T14:02:00Z",
                "speakerId": None,
                "speakerLinkBasis": None,
                "speakerLinkedAt": None,
            },
            {
                "participantName": "Bo Chen",
                "decision": "consent",
                "source": "ui",
                "at": "2026-11-19T14:02:30Z",
                "speakerId": None,
                "speakerLinkBasis": None,
                "speakerLinkedAt": None,
            },
            {
                "participantName": "Owner",
                "decision": "consent",
                "source": "attestation",
                "at": None,
                "speakerId": None,
                "speakerLinkBasis": None,
                "speakerLinkedAt": None,
            },
        ],
        "attestation": None,
        "hasEvidence": True,
        "startedAt": None,
        "updatedAt": None,
    }


def test_the_notices_are_selected_with_their_channel(meeting):
    consent.execute(caller(), "m1")
    query = meeting.calls[0][0]
    assert "Notices {" in query and "Channel" in query and "PostedAt" in query


def test_a_speaker_is_never_matched_to_an_event_by_name(meeting, calls_table):
    """The record holds display names only; a name equal to a speaker's is not a link."""
    calls_table.put(line("r1", "Ana Silva", 1.0))
    answer = consent.execute(caller(), "m1")
    assert {event["speakerId"] for event in answer["events"]} == {None}
    # Nothing is linked, so the transcript is not read.
    assert "trs#m1" not in transcript_reads(calls_table)


def transcript_reads(table):
    """The partition keys the tool queried."""
    return [q["KeyConditionExpression"].get_expression()["values"][1] for q in table.queries]


def line(segment_id, speaker, start, channel="REMOTE"):
    return {
        "PK": "trs#m1",
        "SK": f"ts#{start}#{segment_id}",
        "SegmentId": segment_id,
        "Channel": channel,
        "Speaker": speaker,
        "Transcript": "words",
        "StartTime": start,
        "EndTime": start + 1,
        "IsPartial": False,
    }


LINKED_RECORD = {
    "CallId": "m1",
    "CaptureAllowed": True,
    "Consent": {
        "Mode": "notify",
        "Status": "granted",
        "Events": [
            {
                "ParticipantName": "Bo Chen",
                "Decision": "consent",
                "Source": "voice",
                "RecordedAt": "2026-11-19T14:02:00Z",
                "SpeakerLink": {
                    "Basis": "same_segment",
                    "SegmentId": "r9",
                    "Channel": "REMOTE",
                    "Speaker": "Bo Chen (spk_1)",
                    "LinkedAt": "2026-11-19T14:02:00Z",
                },
            },
            {
                "ParticipantName": "Ana Silva",
                "Decision": "consent",
                "Source": "voice",
                "RecordedAt": "2026-11-19T14:03:00Z",
                "SpeakerLink": {
                    "Basis": "same_segment",
                    "SegmentId": "gone",
                    "Channel": "REMOTE",
                    "Speaker": "Ana Silva (spk_0)",
                    "LinkedAt": "2026-11-19T14:03:00Z",
                },
            },
        ],
    },
}


def test_a_linked_reply_gives_the_speaker_id_the_export_gives(calls_table, appsync):
    calls_table.put(meeting_row())
    calls_table.put(line("r1", "Ana Silva (spk_0)", 1.0))
    calls_table.put(line("r9", "Bo Chen (spk_1)", 9.0))
    appsync.answers["getMeetingConsent"] = {"getMeetingConsent": LINKED_RECORD}
    answer = consent.execute(caller(), "m1")
    bo, ana = answer["events"]
    assert (bo["speakerId"], bo["speakerLinkBasis"], bo["speakerLinkedAt"]) == (
        "remote-spk_1",
        "same_segment",
        "2026-11-19T14:02:00Z",
    )
    # Linked to a line the transcript no longer holds: the link is reported, not its speaker.
    assert (ana["speakerId"], ana["speakerLinkBasis"]) == (None, "same_segment")
    assert "SpeakerLink {" in appsync.calls[0][0]
    assert "trs#m1" in transcript_reads(calls_table)


def test_a_linked_reply_reads_only_the_fields_speaker_ids_need(calls_table, appsync):
    """A long meeting's lines are read without their text; the ids are the same."""
    calls_table.put(meeting_row())
    calls_table.put(line("r1", "Ana Silva (spk_0)", 1.0))
    calls_table.put(line("r9", "Bo Chen (spk_1)", 9.0))
    appsync.answers["getMeetingConsent"] = {"getMeetingConsent": LINKED_RECORD}
    answer = consent.execute(caller(), "m1")
    (read,) = [
        q
        for q in calls_table.queries
        if transcript_reads(SimpleNamespace(queries=[q])) == ["trs#m1"]
    ]
    projected = sorted(read["ExpressionAttributeNames"].values())
    assert projected == sorted(SPEAKER_FIELDS)
    assert "Transcript" not in projected
    assert answer["events"][0]["speakerId"] == "remote-spk_1"


def test_after_the_transcript_is_deleted_a_link_names_no_speaker(calls_table, appsync):
    calls_table.put(meeting_row(TranscriptDeletedAt="2026-11-19T15:00:00Z"))
    calls_table.put(line("r9", "Bo Chen (spk_1)", 9.0))
    appsync.answers["getMeetingConsent"] = {"getMeetingConsent": LINKED_RECORD}
    answer = consent.execute(caller(), "m1")
    assert [e["speakerId"] for e in answer["events"]] == [None, None]
    assert [e["speakerLinkBasis"] for e in answer["events"]] == ["same_segment"] * 2
    assert "trs#m1" not in transcript_reads(calls_table)


BO_ID = "8:acs:0000-bo"


def chat_link_record(with_id=True):
    """getMeetingConsent's answer, which never holds a platform id, and the stored record."""
    link = {
        "Basis": "platform_identity",
        "Channel": "REMOTE",
        "Speaker": "Bo Chen (spk_1)",
        "LinkedAt": "2026-11-19T14:02:00Z",
    }
    answer = {
        "CallId": "m1",
        "CaptureAllowed": True,
        "Consent": {
            "Mode": "notify",
            "Status": "granted",
            "Events": [
                {
                    "EventId": "e1",
                    "ParticipantName": "Bo (guest)",
                    "Decision": "consent",
                    "Source": "chat",
                    "RecordedAt": "2026-11-19T14:02:00Z",
                    "SpeakerLink": {**link, "SegmentId": None},
                }
            ],
        },
    }
    stored_link = {**link, "LinkedBy": "LMA"}
    if with_id:
        stored_link["PlatformParticipantId"] = BO_ID
    stored = {
        "Mode": "notify",
        "Status": "granted",
        "Events": [{"EventId": "e1", "Source": "chat", "SpeakerLink": stored_link}],
    }
    return answer, stored


def id_line(segment_id, speaker, start, platform_id):
    return {**line(segment_id, speaker, start), "PlatformParticipantId": platform_id}


def test_a_chat_reply_linked_by_platform_id_gives_the_speaker_id_and_never_the_id(
    calls_table, appsync
):
    answer, stored = chat_link_record()
    calls_table.put(meeting_row(Consent=stored))
    calls_table.put(line("r1", "Ana Silva (spk_0)", 1.0))
    calls_table.put(id_line("r9", "Bo Chen (spk_1)", 9.0, BO_ID))
    appsync.answers["getMeetingConsent"] = {"getMeetingConsent": answer}
    result = consent.execute(caller(), "m1")
    (event,) = result["events"]
    assert (event["speakerId"], event["speakerLinkBasis"]) == ("remote-spk_1", "platform_identity")
    assert BO_ID not in json.dumps(result)
    assert "EventId" in appsync.calls[0][0]


def test_a_platform_link_whose_stored_id_is_gone_names_no_speaker(calls_table, appsync):
    answer, stored = chat_link_record(with_id=False)
    calls_table.put(meeting_row(Consent=stored))
    calls_table.put(id_line("r9", "Bo Chen (spk_1)", 9.0, BO_ID))
    appsync.answers["getMeetingConsent"] = {"getMeetingConsent": answer}
    (event,) = consent.execute(caller(), "m1")["events"]
    assert (event["speakerId"], event["speakerLinkBasis"]) == (None, "platform_identity")


def test_another_user_is_refused_the_consent_record(meeting):
    with pytest.raises(PermissionError):
        consent.execute(caller(OTHER), "m1")
    assert meeting.calls == []


@pytest.mark.parametrize(
    "state",
    [
        None,
        {"CallId": "m1", "Consent": None, "CaptureAllowed": True},
        {
            "CallId": "m1",
            "Consent": {"Mode": "off", "Status": "not_required", "CaptureAllowed": True},
        },
    ],
)
def test_a_meeting_with_consent_off_returns_mode_off(calls_table, appsync, state):
    calls_table.put(meeting_row())
    appsync.answers["getMeetingConsent"] = {"getMeetingConsent": state}
    answer = consent.execute(caller(), "m1")
    assert answer["mode"] == "off" and answer["rule"] is None
    assert answer["status"] == "not_required"
    assert answer["captureAllowed"] is (None if state is None else True)
    assert answer["notices"] == [] and answer["events"] == [] and answer["hasEvidence"] is False


def test_an_owner_attestation_is_reported(calls_table, appsync):
    calls_table.put(meeting_row())
    record = {
        "Consent": {
            "Mode": "opt_in",
            "Rule": "owner_attests",
            "Status": "granted",
            "Attestation": {
                "AttestedBy": OWNER,
                "Method": "verbal",
                "AttestedAt": "2026-11-19T14:03Z",
            },
            "Events": [
                {
                    "ParticipantName": OWNER,
                    "Decision": "consent",
                    "Source": "attestation",
                    "OccurredAt": "2026-11-19T14:03Z",
                }
            ],
            "Evidence": [],
        }
    }
    appsync.answers["getMeetingConsent"] = {"getMeetingConsent": record}
    answer = consent.execute(caller(), "m1")
    assert answer["rule"] == "owner_attests"
    assert answer["attestation"] == {
        "attestedBy": OWNER,
        "method": "verbal",
        "at": "2026-11-19T14:03Z",
    }
    assert answer["events"][0]["source"] == "attestation"
    assert answer["hasEvidence"] is False


@pytest.mark.parametrize("status", ["pending", "objected"])
def test_a_notify_meeting_has_no_consent_rule(calls_table, appsync, status):
    calls_table.put(meeting_row())
    appsync.answers["getMeetingConsent"] = {
        "getMeetingConsent": {
            "Consent": {"Mode": "notify", "Rule": "each_participant", "Status": status}
        }
    }
    answer = consent.execute(caller(), "m1")
    assert answer["mode"] == "notify" and answer["rule"] is None
    assert answer["status"] == status


# ── share_meeting ───────────────────────────────────────────────────────────


@pytest.fixture
def controls(lambdas):
    lambdas.answers["controls-fn"] = {"Result": "Meetings shared successfully"}
    return lambdas


@pytest.mark.parametrize("who", [caller(OWNER), caller(ADMIN, admin=True)])
def test_share_meeting_adds_and_removes_people(calls_table, controls, who):
    calls_table.put(meeting_row(SharedWith=[SHARED]))
    answer = meeting_actions.share_meeting(who, "m1", ["bo@example.com", SHARED])
    assert answer == {"meetingId": "m1", "sharedWith": [SHARED, "bo@example.com"]}
    _, event = controls.calls[0]
    assert event["info"]["fieldName"] == "shareMeetings"
    assert event["arguments"]["input"]["MeetingRecipients"] == f"{SHARED},bo@example.com"
    answer = meeting_actions.share_meeting(who, "m1", [SHARED], action="remove")
    assert answer["sharedWith"] == []


def test_only_the_owner_or_an_admin_may_share(calls_table, controls):
    calls_table.put(meeting_row())
    with pytest.raises(PermissionError, match="owner or an Admin"):
        meeting_actions.share_meeting(caller(SHARED), "m1", ["bo@example.com"])
    with pytest.raises(PermissionError, match="not found"):
        meeting_actions.share_meeting(caller(OTHER), "m1", ["bo@example.com"])
    assert controls.calls == []


@pytest.mark.parametrize(
    "users,action,message",
    [
        ([], "add", "at least one"),
        (["nobody"], "add", "not an email address"),
        (["bo@example.com"], "toggle", "add or remove"),
        ([f"u{n}@example.com" for n in range(51)], "add", "at most 50"),
    ],
)
def test_share_meeting_checks_its_arguments(calls_table, controls, users, action, message):
    calls_table.put(meeting_row())
    with pytest.raises(ValueError, match=message):
        meeting_actions.share_meeting(caller(), "m1", users, action=action)
    assert controls.calls == []
