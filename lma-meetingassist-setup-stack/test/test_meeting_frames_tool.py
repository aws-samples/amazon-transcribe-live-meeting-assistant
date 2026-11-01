#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""The meeting assistant's get_meeting_frames tool.

What has to hold: the frames a question is about are chosen by its wording
(the last few, a range, a moment); each comes with what was said while it was
on screen; the pictures go to the model only when it reads images, at most a
few at a time; image markdown is offered only where the answer can display it;
a meeting without frames is said to have none; and the pictures sent are
counted for the cost estimate.
"""

from __future__ import annotations

import os
import sys
import types
from typing import Any, Dict, List

import pytest

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

import meeting_frames  # noqa: E402
import strands_meeting_assist_function as handler_module  # noqa: E402


def frame(seconds: float, frame_id: str | None = None) -> Dict[str, Any]:
    fid = frame_id or f"{int(seconds * 1000):010d}-ab12cd"
    return {
        "FrameId": fid,
        "Timestamp": seconds,
        "ImageSignedUrl": f"https://bucket.s3.amazonaws.com/{fid}.jpg?X-Amz-Signature=x",
    }


FRAMES = [frame(30), frame(95), frame(240), frame(600)]
SEGMENTS = [
    {"StartTime": 31, "Speaker": "Alice", "Channel": "CALLER", "Transcript": "This is the budget slide."},
    {"StartTime": 100, "Speaker": "Bob", "Channel": "CALLER", "Transcript": "Hiring is next."},
    {"StartTime": 250, "Speaker": "Assistant", "Channel": "AGENT_ASSISTANT", "Transcript": "Noted."},
]

# ── times ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text, seconds",
    [("12:30", 750), ("1:02:03", 3723), ("754", 754), ("754s", 754), (" [05:07] ", 307), (90, 90.0)],
)
def test_meeting_times_are_read_in_the_forms_people_use(text, seconds):
    assert meeting_frames.parse_meeting_time(text) == seconds


@pytest.mark.parametrize("text", ["", None, "soon", "12:3", "-5", True])
def test_anything_else_is_no_time(text):
    assert meeting_frames.parse_meeting_time(text) is None


# ── choosing frames ─────────────────────────────────────────────────────────


def ids(selected):
    return [f["Timestamp"] for f, _ in selected]


def test_the_last_frames_are_the_most_recent_with_when_each_left_the_screen():
    selected = meeting_frames.select_frames(FRAMES, last=2)
    assert ids(selected) == [240, 600]
    assert [until for _, until in selected] == [600, None]


def test_with_no_question_shape_the_last_frame_is_chosen():
    assert ids(meeting_frames.select_frames(FRAMES)) == [600]


def test_the_frame_on_screen_at_a_moment_is_the_last_one_up_by_then():
    assert ids(meeting_frames.select_frames(FRAMES, at=100)) == [95]
    assert ids(meeting_frames.select_frames(FRAMES, at=10)) == [30], "before the first frame: the first"


def test_a_range_takes_every_frame_on_screen_during_it():
    # 95 went up before 100 and was still up at 100; 600 went up after 300.
    assert ids(meeting_frames.select_frames(FRAMES, start=100, end=300)) == [95, 240]
    # From 500 on: the frame that went up at 240 was still on screen until 600.
    assert ids(meeting_frames.select_frames(FRAMES, start=500)) == [240, 600]
    assert ids(meeting_frames.select_frames(FRAMES, start=700)) == [600]


def test_no_more_than_the_cap_are_chosen_and_frames_come_back_in_time_order():
    many = [frame(t) for t in range(200, 0, -10)]
    selected = meeting_frames.select_frames(many, last=50)
    assert len(selected) == meeting_frames.MAX_FRAMES
    assert ids(selected) == sorted(ids(selected))


# ── the result handed to the model ──────────────────────────────────────────


def result_for(selected, images=None, send_images=True, show_inline=True):
    return meeting_frames.build_tool_result(
        selected,
        SEGMENTS,
        images if images is not None else {f["FrameId"]: b"jpeg" for f, _ in selected},
        total_frames=len(FRAMES),
        send_images=send_images,
        show_inline=show_inline,
    )


def texts(result):
    return "\n".join(b["text"] for b in result["content"] if "text" in b)


def test_each_frame_comes_with_what_was_said_while_it_was_up_and_its_picture():
    result = result_for(meeting_frames.select_frames(FRAMES, last=2)[:1])  # the 240 frame
    assert result["status"] == "success"
    body = texts(result)
    assert "from 04:00 to 10:00" in body
    assert "[04:10] MeetingAssistant: Noted." in body
    assert "budget slide" not in body, "only what was said while this frame was up"
    assert meeting_frames.images_in(result) == 1
    image = [b for b in result["content"] if "image" in b][0]["image"]
    assert image == {"format": "jpeg", "source": {"bytes": b"jpeg"}}


def test_image_markdown_is_offered_only_where_the_answer_can_show_it():
    selected = meeting_frames.select_frames(FRAMES, last=1)
    inline = texts(result_for(selected, show_inline=True))
    assert "![Shared screen at 10:00](keyframe:0000600000-ab12cd)" in inline
    plain = texts(result_for(selected, show_inline=False))
    assert "keyframe:" not in plain
    assert "text only" in plain


def test_a_model_that_cannot_read_images_is_sent_none_and_told_so():
    result = result_for(meeting_frames.select_frames(FRAMES, last=2), send_images=False)
    assert meeting_frames.images_in(result) == 0
    assert "cannot be shown to this model" in texts(result)


def test_only_a_few_pictures_are_sent_at_once():
    many = [frame(t) for t in range(10, 110, 10)]
    selected = meeting_frames.select_frames(many, last=10)
    result = meeting_frames.build_tool_result(
        selected, [], {f["FrameId"]: b"x" for f, _ in selected},
        total_frames=10, send_images=True, show_inline=True,
    )
    assert meeting_frames.images_in(result) == meeting_frames.MAX_IMAGES
    assert "at most 4 are sent at once" in texts(result)


def test_a_picture_that_could_not_be_read_is_said_to_be_missing():
    result = result_for(meeting_frames.select_frames(FRAMES, last=1), images={})
    assert meeting_frames.images_in(result) == 0
    assert "could not be read" in texts(result)


def test_a_meeting_without_frames_is_said_to_have_none():
    result = result_for([])
    assert texts(result) == meeting_frames.NO_FRAMES


def test_the_model_is_told_not_to_identify_people():
    assert "do not identify people" in texts(result_for(meeting_frames.select_frames(FRAMES)))


@pytest.mark.parametrize(
    "model, reads",
    [
        ("global.anthropic.claude-haiku-4-5-20251001-v1:0", True),
        ("us.amazon.nova-lite-v1:0", True),
        ("global.amazon.nova-2-lite-v1:0", True),
        ("amazon.nova-micro-v1:0", False),
    ],
)
def test_which_models_are_sent_pictures(model, reads):
    # Measured: Bedrock rejects an image block sent to Nova Micro.
    assert meeting_frames.model_accepts_images(model) is reads


# ── reading frames ──────────────────────────────────────────────────────────


def test_every_page_of_frames_is_read():
    pages = [
        {"getVideoKeyframes": {"VideoKeyframes": [frame(1)], "nextToken": "t1"}},
        {"getVideoKeyframes": {"VideoKeyframes": [frame(2)], "nextToken": None}},
    ]
    calls: List[Dict[str, Any]] = []

    def execute(_doc, variables):
        calls.append(variables)
        return pages[len(calls) - 1]

    assert [f["Timestamp"] for f in meeting_frames.load_keyframes(execute, "m-1")] == [1, 2]
    assert [c["nextToken"] for c in calls] == [None, "t1"]
    assert all(c["callId"] == "m-1" for c in calls)


def test_only_https_links_are_downloaded():
    assert meeting_frames.download_image("file:///etc/passwd") is None
    assert meeting_frames.download_image(None) is None


# ── the tool, through the handler module ────────────────────────────────────


@pytest.fixture
def tool(monkeypatch):
    strands = types.ModuleType("strands")
    strands.tool = lambda fn: fn
    monkeypatch.setitem(sys.modules, "strands", strands)
    monkeypatch.setattr(handler_module, "APPSYNC_GRAPHQL_URL", "")  # no thinking steps
    monkeypatch.setattr(handler_module.meeting_frames, "load_keyframes", lambda _run, _cid: list(FRAMES))
    monkeypatch.setattr(handler_module.meeting_frames, "download_image", lambda url: b"jpeg-bytes")
    monkeypatch.setattr(handler_module, "query_transcript_items", lambda *_a: list(SEGMENTS))
    metered: List[Any] = []
    monkeypatch.setattr(handler_module.call_metering, "record", lambda *a, **kw: metered.append((a, kw)) or True)

    def make(model_id="global.anthropic.claude-haiku-4-5-20251001-v1:0", show_inline=True):
        return handler_module.create_meeting_frames_tool(
            call_id="m-1", dynamodb_table_name="calls", model_id=model_id, show_inline=show_inline
        ), metered

    return make


def test_the_tool_sends_the_last_two_slides_and_counts_the_pictures(tool):
    get_meeting_frames, metered = tool()
    result = get_meeting_frames(last=2)
    assert meeting_frames.images_in(result) == 2
    assert metered == [
        (("m-1", "MeetingAssistant", "bedrock/global.anthropic.claude-haiku-4-5-20251001-v1:0", {"images": 2}),
         {"source": "strands-meeting-assist"}),
    ]


def test_the_tool_reads_a_moment_given_as_minutes_and_seconds(tool):
    get_meeting_frames, _ = tool()
    result = get_meeting_frames(at_time="1:40")
    assert "Frame 0000095000-ab12cd" in texts(result)


def test_with_a_text_only_model_the_tool_sends_no_pictures_and_meters_none(tool):
    get_meeting_frames, metered = tool(model_id="amazon.nova-micro-v1:0")
    result = get_meeting_frames(last=1)
    assert meeting_frames.images_in(result) == 0
    assert metered == []


def test_a_read_failure_is_an_error_result_not_an_exception(tool, monkeypatch):
    get_meeting_frames, _ = tool()

    def boom(*_a):
        raise RuntimeError("AppSync unavailable")

    monkeypatch.setattr(handler_module.meeting_frames, "load_keyframes", boom)
    result = get_meeting_frames(last=1)
    assert result["status"] == "error"
