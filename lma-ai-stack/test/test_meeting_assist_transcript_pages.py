# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""The Meeting Assistant's read of the current meeting's transcript (no AWS).

`fetch_meeting_transcript` is what the `current_meeting_transcript` tool returns
and what the direct-Bedrock fallback prompts with. It reads the meeting's
segments from DynamoDB with a Query, and a Query returns at most 1 MB of items
*read* per page, applying the FilterExpression only after that cap. The partial
segments this read excludes are numerous, so they spend the page budget without
appearing in the result: a long meeting is spread over several pages. Reading one
page hands the agent the opening stretch of the meeting with nothing to indicate
the rest exists, so every page is read, bounded by a page limit and by a check
that the continuation key is advancing.

The same correction was made to the equivalent read in
`lma-ai-stack/source/lambda_functions/fetch_transcript/index.py`; these tests
assert the Strands function's own copy behaves the same way.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO = Path(__file__).resolve().parents[2]
FUNCTION_SRC = REPO / "lma-meetingassist-setup-stack" / "src"

TABLE = "lma-call-events"
CALL_ID = "call-1"


@pytest.fixture(scope="module")
def strands_module():
    """The meeting-assist Lambda's module, imported by path under its own name."""
    os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
    os.environ.setdefault("AWS_REGION", "us-east-1")
    stub = sys.modules.setdefault("strands", types.ModuleType("strands"))
    stub.tool = lambda func: func

    if str(FUNCTION_SRC) not in sys.path:
        sys.path.append(str(FUNCTION_SRC))
    spec = importlib.util.spec_from_file_location(
        "lma_strands_meeting_assist_pages",
        FUNCTION_SRC / "strands_meeting_assist_function.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _segment(speaker: str, text: str, end_time: float, channel: str = "CALLER") -> dict:
    return {
        "Speaker": speaker,
        "Transcript": text,
        "Channel": channel,
        "EndTime": end_time,
        "IsPartial": False,
    }


def _table_returning(strands_module, monkeypatch, pages: list[dict]) -> MagicMock:
    """Stand in for the DynamoDB table resource, handing back `pages` in order."""
    table = MagicMock()
    table.query.side_effect = pages
    resource = MagicMock()
    resource.Table.return_value = table
    monkeypatch.setattr(strands_module, "dynamodb", resource)
    return table


def test_a_transcript_spanning_several_pages_is_read_to_the_end(strands_module, monkeypatch):
    """The segments on the later pages are the end of the meeting, where decisions land."""
    table = _table_returning(
        strands_module,
        monkeypatch,
        [
            {
                "Items": [_segment("Alice", "first", 1.0)],
                "LastEvaluatedKey": {"PK": "trs#call-1", "SK": "page-1"},
            },
            {
                "Items": [_segment("Bob", "second", 2.0)],
                "LastEvaluatedKey": {"PK": "trs#call-1", "SK": "page-2"},
            },
            {"Items": [_segment("Alice", "third", 3.0)]},
        ],
    )

    transcript = strands_module.fetch_meeting_transcript(CALL_ID, TABLE)

    assert transcript == "Alice: first\nBob: second\nAlice: third"
    assert table.query.call_count == 3


def test_each_page_resumes_from_the_key_the_previous_page_ended_on(strands_module, monkeypatch):
    table = _table_returning(
        strands_module,
        monkeypatch,
        [
            {
                "Items": [_segment("Alice", "first", 1.0)],
                "LastEvaluatedKey": {"PK": "trs#call-1", "SK": "page-1"},
            },
            {"Items": [_segment("Bob", "second", 2.0)]},
        ],
    )

    strands_module.fetch_meeting_transcript(CALL_ID, TABLE)

    first, second = table.query.call_args_list
    assert "ExclusiveStartKey" not in first.kwargs
    assert second.kwargs["ExclusiveStartKey"] == {"PK": "trs#call-1", "SK": "page-1"}


def test_a_single_page_transcript_is_read_with_one_query(strands_module, monkeypatch):
    """Short meetings, which are most of them, cost exactly what they did before."""
    table = _table_returning(
        strands_module, monkeypatch, [{"Items": [_segment("Alice", "all of it", 1.0)]}]
    )

    assert strands_module.fetch_meeting_transcript(CALL_ID, TABLE) == "Alice: all of it"
    assert table.query.call_count == 1


def test_the_assistants_own_turns_are_still_labelled_as_the_assistant(
    strands_module,
    monkeypatch,
):
    """Formatting is unchanged by the paging; the AGENT_ASSISTANT channel is relabelled."""
    _table_returning(
        strands_module,
        monkeypatch,
        [
            {
                "Items": [
                    _segment("Alice", "what did we decide", 1.0),
                    _segment("ignored", "you decided to ship", 2.0, channel="AGENT_ASSISTANT"),
                ]
            }
        ],
    )

    transcript = strands_module.fetch_meeting_transcript(CALL_ID, TABLE)
    assert transcript == "Alice: what did we decide\nMeetingAssistant: you decided to ship"


def test_a_continuation_key_that_stops_advancing_ends_the_read(strands_module, monkeypatch):
    """Asking for the same page again would otherwise spin until the timeout."""
    repeated = {"PK": "trs#call-1", "SK": "page-1"}
    table = _table_returning(
        strands_module,
        monkeypatch,
        [
            {"Items": [_segment("Alice", "first", 1.0)], "LastEvaluatedKey": repeated},
            {"Items": [_segment("Bob", "second", 2.0)], "LastEvaluatedKey": repeated},
        ],
    )

    transcript = strands_module.fetch_meeting_transcript(CALL_ID, TABLE)

    assert table.query.call_count == 2
    assert transcript == "Alice: first\nBob: second"


def test_the_read_stops_at_the_page_limit_and_says_so(strands_module, monkeypatch, caplog):
    """A table that never stops paging must not consume the whole function timeout."""
    table = MagicMock()
    counter = iter(range(10_000))
    table.query.side_effect = lambda **_kwargs: {
        "Items": [_segment("Alice", "again", 1.0)],
        "LastEvaluatedKey": {"PK": "trs#call-1", "SK": f"page-{next(counter)}"},
    }
    resource = MagicMock()
    resource.Table.return_value = table
    monkeypatch.setattr(strands_module, "dynamodb", resource)

    with caplog.at_level("WARNING"):
        strands_module.fetch_meeting_transcript(CALL_ID, TABLE)

    assert table.query.call_count == strands_module.MAX_TRANSCRIPT_PAGES
    assert "page limit" in caplog.text
    assert CALL_ID in caplog.text


def test_a_failed_read_returns_no_transcript_rather_than_raising(strands_module, monkeypatch):
    """Unchanged: the tool reports no transcript and the agent answers without one."""
    table = MagicMock()
    table.query.side_effect = RuntimeError("ProvisionedThroughputExceededException")
    resource = MagicMock()
    resource.Table.return_value = table
    monkeypatch.setattr(strands_module, "dynamodb", resource)

    assert strands_module.fetch_meeting_transcript(CALL_ID, TABLE) == ""
