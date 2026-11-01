# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""ADD_VIDEO_KEYFRAME: what is stored for a key frame, and what is refused.

`build_keyframe_input` touches nothing outside itself, so these run without gql
or the Lambda layer. The handler around it is in test_keyframe_handler.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from keyframe_input import KeyframeEventError, build_keyframe_input  # noqa: E402

EVENT = {
    "EventType": "ADD_VIDEO_KEYFRAME",
    "CallId": "Weekly sync-2026-11-01",
    "FrameId": "0000030000-ab12cd",
    "Timestamp": 30.0,
    "VideoOffset": 41.5,
    "ImageUrl": "https://bucket.s3.us-east-1.amazonaws.com/lma-video-keyframes%2Fx%2Ff.jpg",
    "ThumbnailUrl": "https://bucket.s3.us-east-1.amazonaws.com/lma-video-keyframes%2Fx%2Ff_thumb.jpg",
    "Width": 1568,
    "Height": 882,
    "Source": "VIRTUAL_PARTICIPANT",
    "ShareDetected": True,
    "ChangeScore": 0.05,
    "CreatedAt": "2026-11-01T10:00:00Z",
    "AccessToken": "token",
    "Owner": "someone-else@example.com",
}


def test_a_well_formed_event_becomes_an_input_with_the_meetings_access_and_retention():
    item = build_keyframe_input(
        EVENT, owner="owner@example.com", shared_with="[a@example.com]", expires_after=99
    )
    assert item["Owner"] == "owner@example.com", "the meeting's owner, not the event's"
    assert item["SharedWith"] == "[a@example.com]"
    assert item["ExpiresAfter"] == 99
    assert item["Timestamp"] == 30.0 and item["VideoOffset"] == 41.5
    assert item["Width"] == 1568 and isinstance(item["Width"], int)
    assert "AccessToken" not in item and "EventType" not in item


def test_optional_fields_left_unset_are_not_written():
    event = {
        k: v
        for k, v in EVENT.items()
        if k not in ("VideoOffset", "ShareDetected", "Width", "Height", "ChangeScore")
    }
    item = build_keyframe_input(event, owner="o", shared_with=None, expires_after=1)
    for absent in ("VideoOffset", "ShareDetected", "Width", "Height", "ChangeScore", "SharedWith"):
        assert absent not in item


def test_a_frame_with_no_share_signal_is_stored_without_one():
    item = build_keyframe_input(
        {**EVENT, "ShareDetected": None}, owner="o", shared_with=None, expires_after=1
    )
    assert "ShareDetected" not in item


@pytest.mark.parametrize(
    "change",
    [
        {"CallId": ""},
        {"FrameId": ""},
        {"FrameId": "x" * 65},
        {"Timestamp": None},
        {"Timestamp": -1},
        {"Timestamp": float("nan")},
        {"Timestamp": True},
        {"Timestamp": "30"},
        {"ImageUrl": "s3://bucket/key.jpg"},
        {"ThumbnailUrl": None},
        {"Source": "SOMETHING_ELSE"},
        {"Source": None},
        {"ShareDetected": "yes"},
        {"VideoOffset": -0.5},
    ],
)
def test_an_event_that_cannot_be_stored_is_refused(change):
    with pytest.raises(KeyframeEventError):
        build_keyframe_input({**EVENT, **change}, owner="o", shared_with=None, expires_after=1)
