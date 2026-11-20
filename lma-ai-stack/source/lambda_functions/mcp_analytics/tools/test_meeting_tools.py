# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""The meeting tools: what each sends, and whom each refuses before sending anything.

Scope is enforced once, before any tool runs (index.execute_tool_call, tested in
test_jsonrpc.py); these tests cover each tool's own access rule.
"""

from urllib.parse import parse_qs, urlparse

import pytest

from tools import (
    ask_assistant,
    guidance,
    meeting_actions,
    meeting_agenda,
    meeting_record,
    notifications,
    virtual_participants,
)
from tools._meeting_fakes import ADMIN, OTHER, OWNER, SHARED, caller, meeting_row

# ── push_meeting_guidance / list_meeting_guidance / list_meeting_notifications ─

PUBLISHED = {
    "publishMeetingNotification": {
        "NotificationId": "g1",
        "CreatedAt": "2026-11-19T14:00:00Z",
        "Created": True,
    }
}


def test_guidance_is_published_as_a_guidance_notification(calls_table, appsync, monkeypatch):
    import lma_notifications

    monkeypatch.setattr(lma_notifications, "transcript_expires_after", lambda now=None: 1800000000)
    calls_table.put(meeting_row(SharedWith=[SHARED, "b@example.com"]))
    appsync.answers["publishMeetingNotification"] = PUBLISHED
    answer = guidance.push(
        caller(SHARED), "m1", "  Ask about pricing  ", title="Pricing", level="warning"
    )
    query, variables = appsync.calls[0]
    item = variables["input"]
    assert "mutation PublishMeetingNotification" in query
    assert item["CallId"] == "m1" and item["Body"] == "Ask about pricing"
    assert item["Category"] == "guidance" and item["Type"] == "note"
    assert item["Severity"] == "WARNING" and item["Title"] == "Pricing"
    assert item["Source"] == "mcp" and item["CreatedBy"] == SHARED
    # Guidance follows the transcript's retention.
    assert item["ExpiresAfter"] == 1800000000
    # Who receives it is decided by the resolver from the meeting, not sent.
    assert "Owner" not in item and "SharedWith" not in item
    # The tool's answer keeps its shape.
    assert answer == {
        "meetingId": "m1",
        "guidanceId": "g1",
        "createdAt": "2026-11-19T14:00:00Z",
        "level": "WARNING",
        "title": "Pricing",
        "text": "Ask about pricing",
        "delivered": "Shown on the meeting page to its owner and the people it is shared with.",
    }


def test_guidance_without_a_title_is_published_without_one(calls_table, appsync):
    calls_table.put(meeting_row())
    appsync.answers["publishMeetingNotification"] = PUBLISHED
    answer = guidance.push(caller(), "m1", "note", level="urgent")
    item = appsync.calls[0][1]["input"]
    assert "Title" not in item and item["Severity"] == "URGENT"
    assert answer["title"] is None


def test_guidance_is_never_sent_to_the_meeting_chat(calls_table, appsync, lambdas):
    calls_table.put(meeting_row())
    appsync.answers["publishMeetingNotification"] = PUBLISHED
    guidance.push(caller(), "m1", "note")
    assert [query for query, _ in appsync.calls if "chat" in query.lower()] == []
    assert lambdas.calls == []


@pytest.mark.parametrize(
    "text,level,message",
    [("", None, "text is required"), ("x" * 2001, None, "longer"), ("ok", "loud", "level")],
)
def test_guidance_is_checked_before_anything_is_sent(calls_table, appsync, text, level, message):
    calls_table.put(meeting_row())
    with pytest.raises(ValueError, match=message):
        guidance.push(caller(), "m1", text, level=level)
    assert appsync.calls == []


def test_notifications_of_a_meeting_the_caller_cannot_see_are_refused(calls_table, appsync):
    calls_table.put(meeting_row())
    with pytest.raises(PermissionError):
        guidance.push(caller(OTHER), "m1", "note")
    with pytest.raises(PermissionError):
        guidance.list_items(caller(OTHER), "m1")
    with pytest.raises(PermissionError):
        notifications.list_items(caller(OTHER), "m1")
    assert appsync.calls == []


ROW = {
    "CallId": "m1",
    "NotificationId": "n1",
    "Category": "guidance",
    "Type": "ask_question",
    "Severity": "SUGGESTION",
    "Title": "Ask about the budget",
    "Body": "Nobody has said **who** signs off.",
    "Refs": ["checklist:c1", "segment:s9"],
    "DedupKey": "ask_question:c1",
    "Actions": [{"Label": "Copy question", "Kind": "COPY", "Payload": '{"text":"Who signs?"}'}],
    "Source": "guidance-analyzer",
    "CreatedBy": None,
    "CreatedAt": "2026-11-19T14:00:00Z",
    "UpdatedAt": "2026-11-19T14:03:00Z",
    "Revision": 2,
    "ExpiresAt": None,
}


def test_guidance_is_listed_newest_first_from_the_guidance_category(calls_table, appsync):
    calls_table.put(meeting_row())
    appsync.answers["ListMeetingNotifications"] = {
        "listMeetingNotifications": {"Items": [ROW], "NextToken": None}
    }
    listed = guidance.list_items(caller(SHARED), "m1", limit=500)
    variables = appsync.calls[0][1]
    assert variables["category"] == "guidance" and variables["newestFirst"] is True
    assert variables["limit"] == 200
    assert listed["guidance"] == [
        {
            "guidanceId": "n1",
            "createdAt": "2026-11-19T14:00:00Z",
            "createdBy": None,
            "source": "guidance-analyzer",
            "type": "ask_question",
            "level": "SUGGESTION",
            "title": "Ask about the budget",
            "text": "Nobody has said **who** signs off.",
        }
    ]


def test_notifications_are_listed_with_type_refs_and_dedup_key(calls_table, appsync):
    calls_table.put(meeting_row())
    appsync.answers["ListMeetingNotifications"] = {
        "listMeetingNotifications": {"Items": [ROW], "NextToken": None}
    }
    listed = notifications.list_items(
        caller(SHARED), "m1", category="guidance", since="2026-11-19T13:00:00Z", cursor="c"
    )
    variables = appsync.calls[0][1]
    assert variables == {
        "callId": "m1",
        "limit": 100,
        "newestFirst": False,
        "category": "guidance",
        "since": "2026-11-19T13:00:00Z",
        "nextToken": "c",
    }
    item = listed["notifications"][0]
    assert item["type"] == "ask_question"
    assert item["refs"] == ["checklist:c1", "segment:s9"]
    assert item["dedupKey"] == "ask_question:c1"
    assert item["severity"] == "suggestion" and item["revision"] == 2
    assert item["actions"] == [
        {"label": "Copy question", "kind": "COPY", "payload": {"text": "Who signs?"}}
    ]
    assert listed["nextCursor"] is None and listed["count"] == 1


def test_an_admin_lists_any_meetings_notifications(calls_table, appsync):
    calls_table.put(meeting_row())
    appsync.answers["ListMeetingNotifications"] = {"listMeetingNotifications": {"Items": []}}
    assert notifications.list_items(caller(ADMIN, admin=True), "m1")["count"] == 0


@pytest.mark.parametrize("arguments", [{"limit": "many"}, {"category": 5}, {"since": "x" * 65}])
def test_notification_list_arguments_are_checked(calls_table, appsync, arguments):
    calls_table.put(meeting_row())
    with pytest.raises(ValueError):
        notifications.list_items(caller(), "m1", **arguments)
    assert appsync.calls == []


# ── get_meeting_agenda / update_meeting_agenda ──────────────────────────────


def test_the_agenda_is_written_as_the_meeting_page_writes_it(calls_table, appsync):
    calls_table.put(meeting_row())
    appsync.answers["updateCallAgenda"] = {
        "updateCallAgenda": {
            "CallId": "m1",
            "Agenda": "Intro\nPricing",
            "AgendaSource": "mcp",
            "AgendaUpdatedBy": SHARED,
            "AgendaUpdatedAt": "2026-11-20T10:00:00Z",
        }
    }
    answer = meeting_agenda.update_agenda(caller(SHARED), "m1", ["Intro", "Pricing"])
    query, variables = appsync.calls[0]
    assert "mutation UpdateCallAgenda" in query
    assert variables["input"] == {
        "CallId": "m1",
        "Agenda": "Intro\nPricing",
        "AgendaSource": "mcp",
        "AgendaUpdatedBy": SHARED,
    }
    assert answer == {
        "meetingId": "m1",
        "agenda": "Intro\nPricing",
        "agendaSource": "mcp",
        "updatedBy": SHARED,
        "updatedAt": "2026-11-20T10:00:00Z",
    }


def test_the_agenda_is_read_from_the_meeting(calls_table):
    calls_table.put(
        meeting_row(
            Agenda="Intro",
            AgendaSource="scheduled",
            AgendaUpdatedBy=OWNER,
            AgendaUpdatedAt="2026-11-19T09:00:00Z",
        )
    )
    assert meeting_agenda.get_agenda(caller(SHARED), "m1") == {
        "meetingId": "m1",
        "agenda": "Intro",
        "agendaSource": "scheduled",
        "updatedBy": OWNER,
        "updatedAt": "2026-11-19T09:00:00Z",
    }
    assert meeting_agenda.get_agenda(caller(ADMIN, admin=True), "m1")["agenda"] == "Intro"
    with pytest.raises(PermissionError):
        meeting_agenda.get_agenda(caller(OTHER), "m1")


def test_an_empty_agenda_clears_it_and_a_missing_one_is_refused(calls_table, appsync):
    calls_table.put(meeting_row())
    meeting_agenda.update_agenda(caller(), "m1", "")
    assert appsync.calls[0][1]["input"]["Agenda"] == ""
    with pytest.raises(ValueError):
        meeting_agenda.update_agenda(caller(), "m1", None)


def test_an_ended_meeting_has_its_agenda_changed_and_someone_elses_does_not(calls_table, appsync):
    calls_table.put(meeting_row("m1", status="ENDED"))
    calls_table.put(meeting_row("m2"))
    meeting_agenda.update_agenda(caller(), "m1", "x")
    assert appsync.calls[0][1]["input"]["CallId"] == "m1"
    with pytest.raises(PermissionError):
        meeting_agenda.update_agenda(caller(OTHER), "m2", "x")
    with pytest.raises(ValueError, match="meetingId"):
        meeting_agenda.update_agenda(caller(), "", "x")
    assert len(appsync.calls) == 1


def vp_row(vp_id="vp1", status="SCHEDULED", owner=OWNER, **extra):
    row = {
        "id": vp_id,
        "status": status,
        "Owner": owner,
        "SharedWith": SHARED,
        "isScheduled": status == "SCHEDULED",
        "meetingName": "Weekly",
        "meetingPassword": "secret-passcode",
    }
    row.update(extra)
    return row


# ── end_meeting / list and end Virtual Participants ─────────────────────────


def test_end_meeting_ends_each_unfinished_vp_of_the_meeting(calls_table, vp_table, appsync):
    calls_table.put(meeting_row())
    vp_table.put(vp_row("vp1", status="ACTIVE", CallId="m1"))
    vp_table.put(vp_row("vp0", status="COMPLETED", CallId="m1"))
    vp_table.put(vp_row("vp9", status="ACTIVE", CallId="m9"))
    appsync.answers["endVirtualParticipant"] = {"endVirtualParticipant": {"status": "ENDED"}}
    answer = meeting_actions.end_meeting(caller(), "m1", reason="done")
    assert [vp["virtualParticipantId"] for vp in answer["endedVirtualParticipants"]] == ["vp1"]
    ((query, variables),) = appsync.calls
    assert "mutation EndVirtualParticipant" in query
    assert variables["input"] == {"id": "vp1", "endReason": "done", "endedBy": OWNER}


def test_end_meeting_is_refused_to_a_user_who_does_not_own_the_vp(calls_table, vp_table, appsync):
    calls_table.put(meeting_row())
    vp_table.put(vp_row("vp1", status="ACTIVE", CallId="m1"))
    with pytest.raises(PermissionError):
        meeting_actions.end_meeting(caller(SHARED), "m1")
    with pytest.raises(PermissionError):
        meeting_actions.end_meeting(caller(OTHER), "m1")
    assert appsync.calls == []
    meeting_actions.end_meeting(caller(ADMIN, admin=True), "m1")
    assert len(appsync.calls) == 1


def test_a_meeting_streamed_without_a_vp_is_not_ended_here(calls_table, vp_table, appsync):
    calls_table.put(meeting_row())
    with pytest.raises(ValueError, match="not being captured by a Virtual Participant"):
        meeting_actions.end_meeting(caller(), "m1")
    assert appsync.calls == []


def test_an_incomplete_vp_lookup_is_reported_rather_than_read_as_no_vp(
    calls_table, vp_table, appsync, monkeypatch
):
    """A table larger than the scan bound is said to be, in both tools that scan it."""
    calls_table.put(meeting_row())
    vp_table.put(vp_row("vp1", createdAt="2026-11-19T10:00:00Z"))
    unbounded = vp_table.scan
    monkeypatch.setattr(
        vp_table, "scan", lambda **kw: {**unbounded(**kw), "LastEvaluatedKey": {"id": "more"}}
    )
    with pytest.raises(ValueError, match="larger than one lookup covers"):
        meeting_actions.end_meeting(caller(), "m1")
    listed = virtual_participants.list_vps(caller())
    assert listed["truncated"] is True and "note" in listed
    assert appsync.calls == []


def test_a_complete_vp_list_is_not_marked_truncated(vp_table):
    vp_table.put(vp_row("vp1"))
    assert virtual_participants.list_vps(caller())["truncated"] is False


def test_a_scheduled_vp_is_cancelled(vp_table, appsync):
    vp_table.put(vp_row())
    virtual_participants.end_by_id(caller(), "vp1")
    assert appsync.calls[0][1]["input"]["endReason"].startswith("Cancelled")


def test_vps_are_listed_for_whoever_may_see_them_without_a_password(vp_table):
    vp_table.put(vp_row("vp1", createdAt="2026-11-19T10:00:00Z"))
    vp_table.put(vp_row("vp2", owner=OTHER, SharedWith="", createdAt="2026-11-19T11:00:00Z"))
    vp_table.put(
        vp_row("vp3", owner=OTHER, SharedWith=f"[{SHARED}]", createdAt="2026-11-19T12:00:00Z")
    )
    listed = virtual_participants.list_vps(caller(SHARED))["virtualParticipants"]
    assert [vp["virtualParticipantId"] for vp in listed] == ["vp3", "vp1"]
    assert "secret-passcode" not in repr(listed)
    every = virtual_participants.list_vps(caller(ADMIN, admin=True))["virtualParticipants"]
    assert len(every) == 3


def test_ending_a_finished_vp_again_changes_nothing_and_says_so(vp_table, appsync):
    vp_table.put(vp_row(status="ENDED", CallId="m1"))
    answer = virtual_participants.end_by_id(caller(), "vp1")
    assert answer["alreadyEnded"] is True and answer["status"] == "ENDED"
    assert answer["virtualParticipantId"] == "vp1" and answer["meetingId"] == "m1"
    with pytest.raises(PermissionError):
        virtual_participants.end_by_id(caller(OTHER), "vp1")
    assert appsync.calls == []


# ── delete_meeting ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("confirm", [None, False, "true", 1, "yes"])
def test_delete_meeting_does_nothing_without_confirm_true(calls_table, lambdas, confirm):
    calls_table.put(meeting_row())
    with pytest.raises(ValueError, match="confirm: true"):
        meeting_actions.delete_meeting(caller(), "m1", confirm=confirm)
    assert lambdas.calls == []


def test_delete_meeting_runs_the_meeting_page_deletion_as_the_caller(calls_table, lambdas):
    calls_table.put(meeting_row())
    lambdas.answers["controls-fn"] = {"Result": "Meetings deleted successfully"}
    assert meeting_actions.delete_meeting(caller(), "m1", confirm=True)["deleted"] is True
    ((function, event),) = lambdas.calls
    assert function == "controls-fn"
    assert event["info"] == {"fieldName": "deleteMeetings"}
    assert event["identity"] == {"username": OWNER, "groups": []}
    assert event["arguments"]["input"]["Calls"] == [
        {
            "CallId": "m1",
            "ListPK": "cls#2026-11-19#s#03",
            "ListSK": "ts#2026-11-19T13:45:00.000Z#id#m1",
        }
    ]


def test_delete_meeting_is_for_the_owner_and_admins(calls_table, lambdas):
    calls_table.put(meeting_row())
    lambdas.answers["controls-fn"] = {"Result": "Meetings deleted successfully"}
    with pytest.raises(PermissionError, match="owner or an Admin"):
        meeting_actions.delete_meeting(caller(SHARED), "m1", confirm=True)
    with pytest.raises(PermissionError):
        meeting_actions.delete_meeting(caller(OTHER), "m1", confirm=True)
    assert lambdas.calls == []
    meeting_actions.delete_meeting(caller(ADMIN, admin=True), "m1", confirm=True)
    assert lambdas.calls[0][1]["identity"]["groups"] == ["Admin"]


def test_a_refused_deletion_is_reported(calls_table, lambdas):
    calls_table.put(meeting_row())
    lambdas.answers["controls-fn"] = {"Result": "You don't have permission"}
    with pytest.raises(ValueError, match="permission"):
        meeting_actions.delete_meeting(caller(), "m1", confirm=True)


# ── share_meeting / unshare_meeting ─────────────────────────────────────────


def test_sharing_adds_to_and_unsharing_removes_from_the_share_list(calls_table, lambdas):
    calls_table.put(meeting_row(SharedWith=[SHARED]))
    lambdas.answers["controls-fn"] = {"Result": "Meetings shared successfully"}
    shared = meeting_actions.change_sharing(caller(), "m1", ["new@example.com", SHARED], share=True)
    assert shared["sharedWith"] == [SHARED, "new@example.com"]
    event = lambdas.calls[0][1]
    assert event["info"] == {"fieldName": "shareMeetings"}
    assert event["arguments"]["input"]["MeetingRecipients"] == f"{SHARED},new@example.com"
    meeting_actions.change_sharing(caller(), "m1", [SHARED], share=False)
    assert lambdas.calls[1][1]["arguments"]["input"]["MeetingRecipients"] == ""


def test_sharing_is_for_the_owner_and_admins_and_takes_addresses_only(calls_table, lambdas):
    calls_table.put(meeting_row())
    with pytest.raises(PermissionError):
        meeting_actions.change_sharing(caller(SHARED), "m1", ["a@example.com"], share=True)
    for recipients in (["not an address"], [], "x[y]@a.b", [3]):
        with pytest.raises(ValueError):
            meeting_actions.change_sharing(caller(), "m1", recipients, share=True)
    assert lambdas.calls == []


# ── regenerate_meeting_summary / get_meeting_cost ───────────────────────────


def test_a_summary_is_regenerated_by_the_regenerator_as_the_caller(calls_table, lambdas):
    calls_table.put(meeting_row())
    lambdas.answers["summary-fn"] = {"Success": True}
    assert meeting_actions.regenerate_summary(caller(SHARED), "m1")["started"] is True
    ((function, event),) = lambdas.calls
    assert function == "summary-fn"
    assert event == {
        "arguments": {"input": {"CallId": "m1"}},
        "identity": {"username": SHARED, "groups": []},
    }
    lambdas.answers["summary-fn"] = {
        "Success": False,
        "Message": "a summary is already being regenerated",
    }
    with pytest.raises(ValueError, match="already being regenerated"):
        meeting_actions.regenerate_summary(caller(), "m1")
    with pytest.raises(PermissionError):
        meeting_actions.regenerate_summary(caller(OTHER), "m1")
    assert len(lambdas.calls) == 2


def test_the_cost_is_read_for_callers_get_call_answers(calls_table, appsync):
    calls_table.put(meeting_row())
    appsync.answers["getCallCost"] = {"getCallCost": {"CallId": "m1", "TotalCost": 0.42}}
    assert meeting_actions.meeting_cost(caller(SHARED), "m1")["TotalCost"] == 0.42
    assert appsync.calls[0][1] == {"CallId": "m1"}
    with pytest.raises(PermissionError):
        meeting_actions.meeting_cost(caller(OTHER), "m1")
    assert len(appsync.calls) == 1


# ── ask_meeting_assistant ───────────────────────────────────────────────────


def test_the_assistant_is_asked_as_the_meeting_page_chat_asks_it(calls_table, lambdas):
    calls_table.put(meeting_row())
    lambdas.answers["assistant-fn"] = {"message": "They agreed on Tuesday."}
    answer = ask_assistant.execute(caller(SHARED), "m1", " When is the next call? ")
    assert answer["answer"] == "They agreed on Tuesday."
    ((function, payload),) = lambdas.calls
    assert function == "assistant-fn"
    assert payload["CallId"] == "m1" and payload["Channel"] == "CHAT_ASSISTANT"
    assert payload["Transcript"] == "When is the next call?"
    assert payload["Owner"] == SHARED and payload["IsPartial"] is False


def test_the_assistant_is_not_asked_about_a_hidden_meeting_or_an_empty_question(
    calls_table, lambdas
):
    calls_table.put(meeting_row())
    with pytest.raises(PermissionError):
        ask_assistant.execute(caller(OTHER), "m1", "anything")
    with pytest.raises(ValueError):
        ask_assistant.execute(caller(), "m1", "   ")
    assert lambdas.calls == []


def test_no_answer_from_the_assistant_is_an_error(calls_table, lambdas):
    calls_table.put(meeting_row())
    lambdas.answers["assistant-fn"] = {}
    with pytest.raises(ValueError, match="no answer"):
        ask_assistant.execute(caller(), "m1", "anything")


# ── get_meeting_record ──────────────────────────────────────────────────────

BUCKET = "lma-recordings"


@pytest.fixture
def signed(monkeypatch):
    """S3 signing with fixed credentials, so the links can be inspected."""
    monkeypatch.setenv("RECORDINGS_BUCKET", BUCKET)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIDEXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "example-secret")
    monkeypatch.delenv("AWS_SESSION_TOKEN", raising=False)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.setenv("AWS_REGION", "us-east-1")


def test_the_default_record_is_details_and_summary(calls_table, appsync):
    calls_table.put(
        meeting_row(CallSummaryText="All good. ![Slide](keyframe:0001-ab)", MeetingTopic="Weekly")
    )
    record = meeting_record.execute(caller(SHARED), "m1")
    assert set(record) == {"meetingId", "details", "summary"}
    assert record["details"]["meetingName"] == "Weekly"
    assert record["summary"] == "All good. (Slide)"
    assert appsync.calls == []


def test_recording_links_are_signed_for_an_hour_and_stored_urls_are_not_returned(
    calls_table, appsync, signed
):
    audio = f"https://{BUCKET}.s3.us-east-1.amazonaws.com/lma-audio-recordings/m1.wav"
    video = "https://elsewhere.s3.us-east-1.amazonaws.com/v/m1.mp4"
    calls_table.put(meeting_row(RecordingUrl=audio, VideoRecordingUrl=video))
    record = meeting_record.execute(caller(), "m1", include=["recordings"])
    link = record["recordings"]["audioUrl"]
    query = parse_qs(urlparse(link).query)
    assert urlparse(link).path == "/lma-audio-recordings/m1.wav"
    assert query["X-Amz-Expires"] == ["3600"] and "X-Amz-Signature" in query
    # A URL outside the deployment's recordings bucket is not signed or returned.
    assert record["recordings"]["videoUrl"] is None
    assert video not in repr(record)
    assert record["linksExpireInSeconds"] == 3600 and record["linksExpireAt"].endswith("Z")


def test_key_frames_and_attachments_come_from_their_resolvers_signed_links(calls_table, appsync):
    calls_table.put(meeting_row())
    appsync.answers["getVideoKeyframes"] = {
        "getVideoKeyframes": {
            "VideoKeyframes": [{"FrameId": "f1", "ImageSignedUrl": "https://s?X-Amz-Expires=3600"}]
        }
    }
    appsync.answers["getChatAttachments"] = {
        "getChatAttachments": {
            "ChatAttachments": [{"AttachmentId": "a1", "Title": "Deck"}],
            "nextToken": None,
        }
    }
    record = meeting_record.execute(
        caller(SHARED), "m1", include=["key_frames", "chat_attachments"]
    )
    assert record["keyFrames"][0]["FrameId"] == "f1"
    assert record["chatAttachments"][0]["Title"] == "Deck"
    selections = " ".join(query for query, _ in appsync.calls)
    # The stored, unsigned object locations are not selected.
    assert "ImageUrl" not in selections.replace("ImageSignedUrl", "")
    assert "S3Key" not in selections


def test_the_consent_record_is_for_the_owner_and_admins_only(calls_table, appsync):
    calls_table.put(meeting_row())
    appsync.answers["getMeetingConsent"] = {
        "getMeetingConsent": {"CallId": "m1", "Consent": {"Status": "granted"}}
    }
    with pytest.raises(PermissionError, match="owner and Admins"):
        meeting_record.execute(caller(SHARED), "m1", include=["details", "consent"])
    assert appsync.calls == []
    record = meeting_record.execute(caller(), "m1", include=["consent"])
    assert record["consent"]["Consent"]["Status"] == "granted"
    assert "SignedUrl" in appsync.calls[0][0] and "S3Key" not in appsync.calls[0][0]
    assert meeting_record.execute(caller(ADMIN, admin=True), "m1", include=["consent"])["consent"]


def test_the_transcript_and_chat_links_are_read_from_the_stored_segments(calls_table):
    calls_table.put(meeting_row())
    calls_table.put(
        {
            "PK": "trs#m1",
            "SK": "s#2",
            "SegmentId": "2",
            "StartTime": 5,
            "EndTime": 6,
            "Channel": "MEETING_CHAT",
            "Speaker": "Bo",
            "Transcript": "deck: https://example.com/d",
            "IsPartial": False,
        }
    )
    calls_table.put(
        {
            "PK": "trs#m1",
            "SK": "s#1",
            "SegmentId": "1",
            "StartTime": 1,
            "EndTime": 4,
            "Channel": "REMOTE",
            "Speaker": "Alice",
            "Transcript": "hello",
            "IsPartial": False,
        }
    )
    record = meeting_record.execute(caller(), "m1", include=["transcript", "chat_links"])
    assert [line["text"] for line in record["transcript"]] == [
        "hello",
        "deck: https://example.com/d",
    ]
    assert record["chatLinks"] == [{"url": "https://example.com/d", "sharedBy": "Bo", "time": 5.0}]
    assert "linksExpireAt" not in record


def test_an_unknown_section_or_a_hidden_meeting_is_refused(calls_table, appsync):
    calls_table.put(meeting_row())
    with pytest.raises(ValueError, match="unknown section"):
        meeting_record.execute(caller(), "m1", include=["everything"])
    with pytest.raises(PermissionError):
        meeting_record.execute(caller(OTHER), "m1", include=["recordings"])
    assert appsync.calls == []


def _paged(monkeypatch, pages):
    """read_page answering from `pages` in turn, recording the limit each call asked for."""
    asked = []

    def read_page(
        call_id, *, category=None, since=None, limit=100, cursor=None, newest_first=False
    ):
        asked.append({"limit": limit, "cursor": cursor})
        return pages[len(asked) - 1]

    monkeypatch.setattr(notifications, "read_page", read_page)
    return asked


def test_a_filtered_list_follows_pages_until_it_has_the_limit(calls_table, monkeypatch):
    calls_table.put(meeting_row())
    asked = _paged(
        monkeypatch,
        [
            {"Items": [], "NextToken": "t1"},
            {"Items": [dict(ROW, NotificationId="a")], "NextToken": "t2"},
            {"Items": [dict(ROW, NotificationId="b")], "NextToken": "t3"},
        ],
    )
    listed = notifications.list_items(caller(), "m1", category="guidance", limit=2)
    assert [item["notificationId"] for item in listed["notifications"]] == ["a", "b"]
    # Each page asks only for what is still wanted, so nothing read is left out.
    assert [call["limit"] for call in asked] == [2, 2, 1]
    assert [call["cursor"] for call in asked] == [None, "t1", "t2"]
    assert listed["nextCursor"] == "t3"


def test_a_filtered_list_stops_after_its_page_budget(calls_table, monkeypatch):
    calls_table.put(meeting_row())
    pages = [{"Items": [], "NextToken": f"t{i}"} for i in range(notifications.MAX_PAGES + 5)]
    asked = _paged(monkeypatch, pages)
    listed = notifications.list_items(caller(), "m1", category="guidance")
    assert len(asked) == notifications.MAX_PAGES
    assert listed["count"] == 0 and listed["nextCursor"] == f"t{notifications.MAX_PAGES - 1}"


def test_guidance_is_listed_past_other_categories(calls_table, monkeypatch):
    calls_table.put(meeting_row())
    _paged(
        monkeypatch,
        [
            {"Items": [], "NextToken": "t1"},
            {"Items": [dict(ROW, NotificationId="g")], "NextToken": None},
        ],
    )
    listed = guidance.list_items(caller(), "m1")
    assert [item["guidanceId"] for item in listed["guidance"]] == ["g"]
    assert listed["hasMore"] is False


def test_guidance_says_when_it_stopped_before_the_end(calls_table, monkeypatch):
    calls_table.put(meeting_row())
    pages = [{"Items": [], "NextToken": f"t{i}"} for i in range(notifications.MAX_PAGES)]
    _paged(monkeypatch, pages)
    listed = guidance.list_items(caller(), "m1")
    assert listed["count"] == 0 and listed["hasMore"] is True


# ── the Virtual Participant of each listed meeting ──────────────────────────


def test_vps_by_call_reads_only_the_link_attributes_and_keeps_the_newest(monkeypatch):
    from tools import virtual_participants

    requests = []

    class Table:
        def scan(self, **kwargs):
            requests.append(kwargs)
            return {
                "Items": [
                    {"id": "old", "CallId": "m1", "createdAt": "2026-11-19T09:00:00Z"},
                    {"id": "new", "CallId": "m1", "createdAt": "2026-11-19T10:00:00Z"},
                    {"id": "other", "CallId": "m9", "createdAt": "2026-11-19T10:00:00Z"},
                ]
            }

    monkeypatch.setattr(virtual_participants, "vp_table", lambda: Table())
    found, complete = virtual_participants.vps_by_call({"m1"})
    assert found == {"m1": "new"} and complete is True
    assert requests[0]["ProjectionExpression"] == "#id, #call, #created"
    assert requests[0]["ExpressionAttributeNames"] == {
        "#id": "id",
        "#call": "CallId",
        "#created": "createdAt",
    }
