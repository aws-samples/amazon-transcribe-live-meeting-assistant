# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""get_meeting_consent and share_meeting."""

import pytest

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
            },
            {
                "participantName": "Ana Silva",
                "decision": "consent",
                "source": "voice",
                "at": "2026-11-19T14:02:00Z",
                "speakerId": None,
            },
            {
                "participantName": "Bo Chen",
                "decision": "consent",
                "source": "ui",
                "at": "2026-11-19T14:02:30Z",
                "speakerId": None,
            },
            {
                "participantName": "Owner",
                "decision": "consent",
                "source": "attestation",
                "at": None,
                "speakerId": None,
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


def test_a_speaker_is_never_matched_to_an_event_by_name(meeting):
    """The record holds display names only; a name equal to a speaker's is not a link."""
    answer = consent.execute(caller(), "m1")
    assert {event["speakerId"] for event in answer["events"]} == {None}


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
