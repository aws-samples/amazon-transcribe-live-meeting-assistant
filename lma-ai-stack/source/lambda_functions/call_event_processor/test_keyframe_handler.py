# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""The ADD_VIDEO_KEYFRAME handler, driven through the real call_event_processor.

The query is built against the deployed schema and only the GraphQL transport is
replaced, as in test_graphql_response_handling.py, whose module setup this
reuses. Pinned here: the frame takes the meeting's access fields rather than
anything the event claims; a missing meeting fails the record; an event that can
never be stored is dropped without reading or writing anything.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from typing import Any, List

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

# Skips this module, as that one skips itself, where gql or the layer is missing.
from test_graphql_response_handling import cep, print_ast, run, schema  # noqa: E402,F401
from test_keyframe_input import EVENT  # noqa: E402


class SequenceSession:
    """An AppSync session that answers each query with the next queued response."""

    def __init__(self, gql_schema: Any, responses: List[Any]) -> None:
        self.client = types.SimpleNamespace(schema=gql_schema)
        self.responses = list(responses)
        self.queries: List[Any] = []

    async def execute(self, query: Any) -> Any:
        self.queries.append(query)
        return self.responses.pop(0)


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


def test_the_handler_fails_the_record_when_the_meeting_is_not_there(schema):  # noqa: F811
    session = SequenceSession(schema, [{"getCall": None}])
    with pytest.raises(Exception):
        run(cep.execute_add_video_keyframe_mutation(message=dict(EVENT), appsync_session=session))
    assert len(session.queries) == 1, "nothing is written for a meeting that does not exist"


def test_the_handler_drops_an_event_that_can_never_be_stored(schema):  # noqa: F811
    """Dropped, not raised: a raised error is redelivered and holds up the shard."""
    session = SequenceSession(schema, [])
    result = run(
        cep.execute_add_video_keyframe_mutation(
            message={**EVENT, "ImageUrl": "s3://bucket/key.jpg"}, appsync_session=session
        )
    )
    assert result == {"ok": False, "Dropped": True}
    assert session.queries == [], "nothing is read or written for it"
    assert any("cannot be stored" in m for m in cep.LOGGER.at_level("ERROR"))


def test_a_stored_frame_has_its_text_read_once_stored(schema, monkeypatch):  # noqa: F811
    invoked = []
    monkeypatch.setattr(
        cep, "KEYFRAME_TEXT_EXTRACTOR_ARN", "arn:aws:lambda:us-east-1:123456789012:function:x"
    )
    monkeypatch.setattr(
        cep,
        "LAMBDA_HOOK_CLIENT",
        type("C", (), {"invoke": lambda self, **kw: invoked.append(kw)})(),
        raising=False,
    )
    session = SequenceSession(
        schema,
        [
            {"getCall": {"CallId": EVENT["CallId"], "Owner": "o", "SharedWith": None}},
            {"addVideoKeyframe": {"CallId": EVENT["CallId"], "FrameId": EVENT["FrameId"]}},
        ],
    )
    run(cep.execute_add_video_keyframe_mutation(message=dict(EVENT), appsync_session=session))
    assert len(invoked) == 1
    assert invoked[0]["InvocationType"] == "Event"
    payload = json.loads(invoked[0]["Payload"])
    assert payload == {
        "CallId": EVENT["CallId"],
        "FrameId": EVENT["FrameId"],
        "ImageUrl": EVENT["ImageUrl"],
    }


def test_a_failed_queue_does_not_fail_the_frame(schema, monkeypatch):  # noqa: F811
    def boom(self, **_kw):
        raise RuntimeError("throttled")

    monkeypatch.setattr(
        cep, "KEYFRAME_TEXT_EXTRACTOR_ARN", "arn:aws:lambda:us-east-1:123456789012:function:x"
    )
    monkeypatch.setattr(cep, "LAMBDA_HOOK_CLIENT", type("C", (), {"invoke": boom})(), raising=False)
    session = SequenceSession(
        schema,
        [
            {"getCall": {"CallId": EVENT["CallId"], "Owner": "o", "SharedWith": None}},
            {"addVideoKeyframe": {"CallId": EVENT["CallId"], "FrameId": EVENT["FrameId"]}},
        ],
    )
    result = run(
        cep.execute_add_video_keyframe_mutation(message=dict(EVENT), appsync_session=session)
    )
    assert result == {"addVideoKeyframe": {"CallId": EVENT["CallId"], "FrameId": EVENT["FrameId"]}}
