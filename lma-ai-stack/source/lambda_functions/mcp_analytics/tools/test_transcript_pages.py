# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""get_meeting_transcript, format json: a meeting's transcript page by page, with a nextToken."""

import base64
import json

import pytest

from tools import get_transcript, transcript_pages
from tools._meeting_fakes import ADMIN, OTHER, SHARED, caller, meeting_row


def segment(segment_id, start, end, text="", channel="REMOTE", speaker="Alice", call_id="m1"):
    return {
        "PK": f"trs#{call_id}",
        "SK": f"s#{segment_id}",
        "CallId": call_id,
        "SegmentId": segment_id,
        "StartTime": start,
        "EndTime": end,
        "Channel": channel,
        "Speaker": speaker,
        "Transcript": text or segment_id,
        "IsPartial": False,
    }


@pytest.fixture
def meeting(calls_table):
    calls_table.put(meeting_row("m1"))
    return calls_table


def poll(since=None, user=None, **kwargs):
    who = caller(user) if user else caller()
    return transcript_pages.execute(who, "m1", next_token=since, **kwargs)


def ids(answer):
    return [line["segmentId"] for line in answer["segments"]]


def test_the_first_poll_returns_speech_and_chat_in_the_order_they_end(meeting):
    meeting.put(segment("b", 5, 9))
    meeting.put(segment("a", 0, 4, channel="LOCAL", speaker="Bob"))
    meeting.put(segment("c", 6, 6, text="see https://example.com", channel="MEETING_CHAT"))
    meeting.put(segment("x", 7, 7, channel="CHAT_ASSISTANT", speaker="Meeting Assistant"))
    answer = poll()
    assert ids(answer) == ["a", "c", "b"]
    assert [line["type"] for line in answer["segments"]] == ["speech", "chat", "speech"]
    assert answer["meetingStatus"] == "STARTED" and answer["pollAfterSeconds"] == 5
    assert answer["hasMore"] is False
    # Each segment has the fields the tool promises.
    assert set(answer["segments"][0]) >= {
        "segmentId",
        "startTime",
        "endTime",
        "speaker",
        "channel",
        "text",
        "type",
    }
    # Only final segments are read.
    assert "FilterExpression" in meeting.queries[0]


def test_a_poll_with_the_cursor_returns_only_lines_added_since(meeting):
    meeting.put(segment("a", 0, 4))
    first = poll()
    meeting.put(segment("b", 5, 9))
    second = poll(first["nextToken"])
    assert ids(second) == ["b"]
    third = poll(second["nextToken"])
    assert ids(third) == []
    # While the meeting is live a token is always returned, so a caller can poll on.
    assert third["nextToken"]


def test_a_line_finalized_late_on_the_other_channel_is_still_returned(meeting):
    meeting.put(segment("remote", 10, 20))
    first = poll()
    # The local channel's line ends before the one already returned.
    meeting.put(segment("local", 12, 15, channel="LOCAL"))
    second = poll(first["nextToken"])
    assert ids(second) == ["local"]
    assert ids(poll(second["nextToken"])) == []


def test_a_truncated_answer_says_so_and_the_next_poll_skips_nothing(meeting):
    for index in range(5):
        meeting.put(segment(f"s{index}", index, index + 0.5))
    first = poll(max_segments=2)
    assert ids(first) == ["s0", "s1"] and first["hasMore"] is True
    assert first["pollAfterSeconds"] is None
    second = poll(first["nextToken"], max_segments=2)
    assert ids(second) == ["s2", "s3"]
    third = poll(second["nextToken"], max_segments=2)
    assert ids(third) == ["s4"] and third["hasMore"] is False


def test_lines_with_the_same_end_time_are_not_lost_at_a_truncation(meeting):
    for name in ("a", "b", "c"):
        meeting.put(segment(name, 1, 2))
    first = poll(max_segments=1)
    second = poll(first["nextToken"], max_segments=5)
    assert sorted(ids(first) + ids(second)) == ["a", "b", "c"]


def test_the_assistant_answers_are_returned_only_when_asked_for(meeting):
    meeting.put(segment("x", 1, 1, channel="CHAT_ASSISTANT"))
    assert ids(poll()) == []
    assert ids(poll(include_assistant=True)) == ["x"]


def test_an_ended_meeting_read_to_its_last_segment_has_no_next_token(calls_table):
    calls_table.put(meeting_row("m1", status="ENDED"))
    for index in range(3):
        calls_table.put(segment(f"s{index}", index, index + 0.5))
    first = poll(max_segments=2)
    assert ids(first) == ["s0", "s1"] and first["nextToken"]
    last = poll(first["nextToken"], max_segments=2)
    assert ids(last) == ["s2"]
    assert last["nextToken"] is None and last["pollAfterSeconds"] is None
    assert last["meetingStatus"] == "ENDED"


def test_a_meeting_that_ends_while_being_read_ends_the_tokens(calls_table):
    row = meeting_row("m1", status="STARTED")
    calls_table.put(row)
    calls_table.put(segment("a", 0, 1))
    first = poll()
    assert first["nextToken"]
    row["Status"] = "ENDED"
    calls_table.put(segment("b", 2, 3))
    last = poll(first["nextToken"])
    assert ids(last) == ["b"] and last["nextToken"] is None


def test_max_segments_defaults_to_500_and_is_at_most_2000(meeting):
    for index in range(2100):
        meeting.put(segment(f"s{index:04d}", index, index + 0.5))
    assert len(poll()["segments"]) == 500
    assert len(poll(max_segments=5000)["segments"]) == 2000
    with pytest.raises(ValueError, match="maxSegments"):
        poll(max_segments="many")


def test_a_deleted_transcript_says_so(calls_table):
    calls_table.put(meeting_row("m1", status="ENDED", TranscriptDeletedAt="2026-11-19T15:00:00Z"))
    answer = poll()
    assert answer["segments"] == [] and answer["nextToken"] is None
    assert answer["transcriptDeleted"] is True


def test_the_json_format_of_get_meeting_transcript_pages(meeting, monkeypatch):
    monkeypatch.setenv("LMA_WEB_APP_URL", "https://lma.example.com")
    meeting.put(segment("a", 0, 1))
    answer = get_transcript.execute("m1", format="json", user_id=SHARED, max_segments=10)
    assert ids(answer) == ["a"] and answer["format"] == "json"
    assert answer["meetingId"] == "m1" and answer["nextToken"]
    with pytest.raises(PermissionError):
        get_transcript.execute("m1", format="json", user_id=OTHER)


def test_a_cursor_of_another_meeting_or_of_no_meeting_is_refused(meeting):
    other = transcript_pages.encode_cursor("m2", 5.0, [])
    with pytest.raises(ValueError, match="different meeting"):
        poll(other)
    forged = base64.urlsafe_b64encode(json.dumps({"v": 1, "m": "m1"}).encode()).decode()
    for token in ("not-a-token", forged, "!!!"):
        with pytest.raises(ValueError, match="nextToken"):
            poll(token)


def test_the_cursor_holds_only_the_lines_inside_the_look_back_window(meeting):
    meeting.put(segment("old", 0, 1))
    meeting.put(segment("new", 200, 201))
    state = json.loads(base64.urlsafe_b64decode(poll()["nextToken"]))
    assert state["t"] == 201 and state["ids"] == ["new"]


def test_shared_users_and_admins_poll_and_others_are_refused(meeting):
    meeting.put(segment("a", 0, 1))
    assert ids(poll(user=SHARED)) == ["a"]
    assert ids(transcript_pages.execute(caller(ADMIN, admin=True), "m1")) == ["a"]
    with pytest.raises(PermissionError):
        poll(user=OTHER)


def test_a_read_key_polls(meeting):
    meeting.put(segment("a", 0, 1))
    assert ids(transcript_pages.execute(caller(scope="read"), "m1")) == ["a"]
