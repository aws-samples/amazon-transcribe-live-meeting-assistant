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
        "Participants": ["Ana Silva", "Bo Chen", "Unidentified participant 1"],
        "ParticipantsSeen": ["Ana Silva", "Bo Chen", "Cy Diaz"],
        "Events": [
            {
                "ParticipantName": "ana silva",
                "Decision": "objection",
                "Source": "chat",
                "RecordedAt": "2026-11-19T14:01:00Z",
            },
            {
                "ParticipantName": "Ana Silva",
                "Decision": "consent",
                "Source": "voice",
                "RecordedAt": "2026-11-19T14:02:00Z",
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
    assert answer["mode"] == "opt-in" and answer["consentGivenBy"] == "each_participant"
    assert answer["noticePostedAt"] == "2026-11-19T14:00:00Z"
    assert answer["evidence"] == {"exists": True, "count": 2}
    assert answer["participants"] == [
        {
            "name": "Ana Silva",
            "status": "consented",
            "at": "2026-11-19T14:02:00Z",
            "method": "voice",
        },
        {"name": "Bo Chen", "status": "no_reply", "at": None, "method": None},
        {"name": "Cy Diaz", "status": "no_reply", "at": None, "method": None},
    ]
    assert "Evidence" not in str(answer["participants"])


def test_another_user_is_refused_the_consent_record(meeting):
    with pytest.raises(PermissionError):
        consent.execute(caller(OTHER), "m1")
    assert meeting.calls == []


@pytest.mark.parametrize(
    "state", [None, {"CallId": "m1", "Consent": None}, {"CallId": "m1", "Consent": {"Mode": "off"}}]
)
def test_a_meeting_with_consent_off_returns_mode_off(calls_table, appsync, state):
    calls_table.put(meeting_row())
    appsync.answers["getMeetingConsent"] = {"getMeetingConsent": state}
    assert consent.execute(caller(), "m1") == {"meetingId": "m1", "mode": "off"}


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
            "Evidence": [],
        }
    }
    appsync.answers["getMeetingConsent"] = {"getMeetingConsent": record}
    answer = consent.execute(caller(), "m1")
    assert answer["consentGivenBy"] == "owner_attests"
    assert answer["attestation"] == {
        "attestedBy": OWNER,
        "method": "verbal",
        "at": "2026-11-19T14:03Z",
    }
    assert answer["evidence"] == {"exists": False, "count": 0}


def test_a_notify_meeting_has_no_consent_rule(calls_table, appsync):
    calls_table.put(meeting_row())
    appsync.answers["getMeetingConsent"] = {
        "getMeetingConsent": {"Consent": {"Mode": "notify", "Rule": None, "Status": "not_required"}}
    }
    answer = consent.execute(caller(), "m1")
    assert answer["mode"] == "notify" and answer["consentGivenBy"] is None


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
