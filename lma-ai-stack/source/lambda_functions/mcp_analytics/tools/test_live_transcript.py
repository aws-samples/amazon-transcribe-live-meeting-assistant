# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""get_live_transcript: polling a meeting's transcript with a cursor."""

import base64
import json

import pytest

from tools import live_transcript
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
    return live_transcript.execute(who, "m1", since=since, **kwargs)


def ids(answer):
    return [line["segmentId"] for line in answer["lines"]]


def test_the_first_poll_returns_speech_and_chat_in_the_order_they_end(meeting):
    meeting.put(segment("b", 5, 9))
    meeting.put(segment("a", 0, 4, channel="LOCAL", speaker="Bob"))
    meeting.put(segment("c", 6, 6, text="see https://example.com", channel="MEETING_CHAT"))
    meeting.put(segment("x", 7, 7, channel="CHAT_ASSISTANT", speaker="Meeting Assistant"))
    answer = poll()
    assert ids(answer) == ["a", "c", "b"]
    assert [line["type"] for line in answer["lines"]] == ["speech", "chat", "speech"]
    assert answer["live"] is True and answer["pollAfterSeconds"] == 5
    assert answer["hasMore"] is False
    # Only final segments are read.
    assert "FilterExpression" in meeting.queries[0]


def test_a_poll_with_the_cursor_returns_only_lines_added_since(meeting):
    meeting.put(segment("a", 0, 4))
    first = poll()
    meeting.put(segment("b", 5, 9))
    second = poll(first["cursor"])
    assert ids(second) == ["b"]
    third = poll(second["cursor"])
    assert ids(third) == []


def test_a_line_finalized_late_on_the_other_channel_is_still_returned(meeting):
    meeting.put(segment("remote", 10, 20))
    first = poll()
    # The local channel's line ends before the one already returned.
    meeting.put(segment("local", 12, 15, channel="LOCAL"))
    second = poll(first["cursor"])
    assert ids(second) == ["local"]
    assert ids(poll(second["cursor"])) == []


def test_a_truncated_answer_says_so_and_the_next_poll_skips_nothing(meeting):
    for index in range(5):
        meeting.put(segment(f"s{index}", index, index + 0.5))
    first = poll(limit=2)
    assert ids(first) == ["s0", "s1"] and first["hasMore"] is True
    second = poll(first["cursor"], limit=2)
    assert ids(second) == ["s2", "s3"]
    third = poll(second["cursor"], limit=2)
    assert ids(third) == ["s4"] and third["hasMore"] is False


def test_lines_with_the_same_end_time_are_not_lost_at_a_truncation(meeting):
    for name in ("a", "b", "c"):
        meeting.put(segment(name, 1, 2))
    first = poll(limit=1)
    second = poll(first["cursor"], limit=5)
    assert sorted(ids(first) + ids(second)) == ["a", "b", "c"]


def test_the_assistant_answers_are_returned_only_when_asked_for(meeting):
    meeting.put(segment("x", 1, 1, channel="CHAT_ASSISTANT"))
    assert ids(poll()) == []
    assert ids(poll(include_assistant=True)) == ["x"]


def test_an_ended_meeting_is_not_live(calls_table):
    calls_table.put(meeting_row("m1", status="ENDED"))
    answer = poll()
    assert answer["live"] is False and answer["pollAfterSeconds"] is None


def test_a_cursor_of_another_meeting_or_of_no_meeting_is_refused(meeting):
    other = live_transcript.encode_cursor("m2", 5.0, [])
    with pytest.raises(ValueError, match="different meeting"):
        poll(other)
    forged = base64.urlsafe_b64encode(json.dumps({"v": 1, "m": "m1"}).encode()).decode()
    for cursor in ("not-a-cursor", forged, "!!!"):
        with pytest.raises(ValueError, match="cursor"):
            poll(cursor)


def test_the_cursor_holds_only_the_lines_inside_the_look_back_window(meeting):
    meeting.put(segment("old", 0, 1))
    meeting.put(segment("new", 200, 201))
    state = json.loads(base64.urlsafe_b64decode(poll()["cursor"]))
    assert state["t"] == 201 and state["ids"] == ["new"]


def test_shared_users_and_admins_poll_and_others_are_refused(meeting):
    meeting.put(segment("a", 0, 1))
    assert ids(poll(user=SHARED)) == ["a"]
    assert ids(live_transcript.execute(caller(ADMIN, admin=True), "m1")) == ["a"]
    with pytest.raises(PermissionError):
        poll(user=OTHER)


def test_a_read_key_polls(meeting):
    meeting.put(segment("a", 0, 1))
    assert ids(live_transcript.execute(caller(scope="read"), "m1")) == ["a"]
