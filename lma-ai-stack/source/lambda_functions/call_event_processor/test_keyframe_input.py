# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""ADD_VIDEO_KEYFRAME: what is stored for a key frame, and what is refused.

`build_keyframe_input` is tested directly (it touches nothing outside itself).
The handler around it is driven once through the real call_event_processor
module, with the query built against the deployed schema and only the GraphQL
transport replaced, to show that the frame takes the meeting's access fields
rather than anything the event claims.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, List

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
        {**EVENT, "ShareDetected": None, "Source": "DESKTOP_CAPTURE"},
        owner="o",
        shared_with=None,
        expires_after=1,
    )
    assert "ShareDetected" not in item
    assert item["Source"] == "DESKTOP_CAPTURE"


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
        {"ShareDetected": "yes"},
        {"VideoOffset": -0.5},
    ],
)
def test_an_event_that_cannot_be_stored_is_refused(change):
    with pytest.raises(KeyframeEventError):
        build_keyframe_input({**EVENT, **change}, owner="o", shared_with=None, expires_after=1)


# ── the handler, through the real module ────────────────────────────────────

try:
    from test_graphql_response_handling import cep, print_ast, run, schema  # noqa: E402,F401
except Exception:  # pragma: no cover - environment without gql / the layer
    cep = None


class SequenceSession:
    """An AppSync session that answers each query with the next queued response."""

    def __init__(self, schema: Any, responses: List[Any]) -> None:
        import types

        self.client = types.SimpleNamespace(schema=schema)
        self.responses = list(responses)
        self.queries: List[Any] = []

    async def execute(self, query: Any) -> Any:
        self.queries.append(query)
        return self.responses.pop(0)


@pytest.mark.skipif(cep is None, reason="needs gql, graphql-core and the layer")
def test_the_handler_reads_the_meetings_access_then_stores_the_frame(schema):  # noqa: F811
    session = SequenceSession(
        schema,
        [
            {
                "getCall": {
                    "CallId": EVENT["CallId"],
                    "Owner": "owner@example.com",
                    "SharedWith": "[a@example.com]",
                }
            },
            {"addVideoKeyframe": {"CallId": EVENT["CallId"], "FrameId": EVENT["FrameId"]}},
        ],
    )
    run(cep.execute_add_video_keyframe_mutation(message=dict(EVENT), appsync_session=session))
    assert len(session.queries) == 2
    mutation = print_ast(session.queries[1])
    assert "addVideoKeyframe" in mutation
    assert '"owner@example.com"' in mutation
    assert "someone-else@example.com" not in mutation
    assert "AccessToken" not in mutation
    # The subscription delivers only what the mutation selected; its filter needs these.
    for field in ("Owner", "SharedWith", "ThumbnailUrl", "Timestamp"):
        assert field in mutation.split("{", 2)[-1]


@pytest.mark.skipif(cep is None, reason="needs gql, graphql-core and the layer")
def test_the_handler_fails_the_record_when_the_meeting_is_not_there(schema):  # noqa: F811
    session = SequenceSession(schema, [{"getCall": None}])
    with pytest.raises(Exception):
        run(cep.execute_add_video_keyframe_mutation(message=dict(EVENT), appsync_session=session))
    assert len(session.queries) == 1, "nothing is written for a meeting that does not exist"
