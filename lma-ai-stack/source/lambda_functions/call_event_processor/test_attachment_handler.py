# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""The ADD_CHAT_ATTACHMENT handler, driven through the real call_event_processor.

As test_keyframe_handler.py: the query is built against the deployed schema and
only the GraphQL transport is replaced. Pinned here: the attachment takes the
meeting's access fields rather than anything the event claims; a missing
meeting fails the record; an event that can never be stored is dropped without
reading or writing anything; the summary is queued only for an attachment that
asks for one.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

# Skips this module, as that one skips itself, where gql or the layer is missing.
from test_attachment_input import EVENT  # noqa: E402
from test_graphql_response_handling import cep, print_ast, run, schema  # noqa: E402,F401
from test_keyframe_handler import SequenceSession  # noqa: E402

ARN = "arn:aws:lambda:us-east-1:123456789012:function:summarizer"


def _session(gql_schema):
    return SequenceSession(
        gql_schema,
        [
            {
                "getCall": {
                    "CallId": EVENT["CallId"],
                    "Owner": "owner@example.com",
                    "SharedWith": "[a@example.com]",
                }
            },
            {
                "addChatAttachment": {
                    "CallId": EVENT["CallId"],
                    "AttachmentId": EVENT["AttachmentId"],
                }
            },
        ],
    )


def _recording_lambda(monkeypatch):
    invoked = []
    monkeypatch.setattr(cep, "CHAT_ATTACHMENT_SUMMARIZER_ARN", ARN)
    monkeypatch.setattr(
        cep,
        "LAMBDA_HOOK_CLIENT",
        type("C", (), {"invoke": lambda self, **kw: invoked.append(kw)})(),
        raising=False,
    )
    return invoked


def test_the_handler_stores_the_attachment_with_the_meetings_access(schema, monkeypatch):  # noqa: F811
    _recording_lambda(monkeypatch)
    session = _session(schema)
    run(cep.execute_add_chat_attachment_mutation(message=dict(EVENT), appsync_session=session))
    assert len(session.queries) == 2
    mutation = print_ast(session.queries[1])
    assert "addChatAttachment" in mutation
    assert '"owner@example.com"' in mutation
    assert "someone-else@example.com" not in mutation
    assert "AccessToken" not in mutation


def test_a_stored_attachment_has_its_summary_queued_once_stored(schema, monkeypatch):  # noqa: F811
    invoked = _recording_lambda(monkeypatch)
    run(
        cep.execute_add_chat_attachment_mutation(
            message=dict(EVENT), appsync_session=_session(schema)
        )
    )
    assert len(invoked) == 1
    assert invoked[0]["FunctionName"] == ARN
    assert invoked[0]["InvocationType"] == "Event"
    assert json.loads(invoked[0]["Payload"]) == {
        "CallId": EVENT["CallId"],
        "AttachmentId": EVENT["AttachmentId"],
    }


def test_no_summary_is_queued_when_the_switch_is_off_or_nothing_was_stored(schema, monkeypatch):  # noqa: F811
    invoked = _recording_lambda(monkeypatch)
    run(
        cep.execute_add_chat_attachment_mutation(
            message={**EVENT, "Summarize": False}, appsync_session=_session(schema)
        )
    )
    run(
        cep.execute_add_chat_attachment_mutation(
            message={**EVENT, "Status": "NOT_STORED", "NotStoredReason": "NOT_DOWNLOADABLE"},
            appsync_session=_session(schema),
        )
    )
    assert invoked == []


def test_the_handler_fails_the_record_when_the_meeting_is_not_there(schema):  # noqa: F811
    session = SequenceSession(schema, [{"getCall": None}])
    with pytest.raises(Exception):
        run(cep.execute_add_chat_attachment_mutation(message=dict(EVENT), appsync_session=session))
    assert len(session.queries) == 1, "nothing is written for a meeting that does not exist"


def test_the_handler_drops_an_event_that_can_never_be_stored(schema):  # noqa: F811
    session = SequenceSession(schema, [])
    result = run(
        cep.execute_add_chat_attachment_mutation(
            message={**EVENT, "S3Key": "lma-audio-recordings/x.wav"}, appsync_session=session
        )
    )
    assert result == {"ok": False, "Dropped": True}
    assert session.queries == [], "nothing is read or written for it"


def test_the_event_type_is_routed_to_the_attachment_handler():
    source = (HERE / "event_processor" / "call_event_processor.py").read_text(encoding="utf-8")
    assert 'ADD_CHAT_ATTACHMENT="ADD_CHAT_ATTACHMENT"' in source
    assert (
        "execute_add_chat_attachment_mutation("
        in source.split('event_type == "ADD_CHAT_ATTACHMENT"')[1][:400]
    )
