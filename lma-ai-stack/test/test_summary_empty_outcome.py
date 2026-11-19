# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Static contract tests for the "nothing to summarise" outcome (no AWS).

The Bedrock summariser answers a meeting with no speech, chat or screen text
with one fixed sentence instead of calling the model. The end-of-meeting
pipeline and the summary regenerator recognise that sentence and record the
summary's state as "empty"; the meeting page reads that status. Each Lambda
holds its own copy of the sentence and of the status name, because they share
no module, so these tests hold the copies to one another.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

AI_STACK = Path(__file__).resolve().parents[1]
FUNCTIONS = AI_STACK / "source" / "lambda_functions"
SUMMARISER = FUNCTIONS / "bedrock_summary_lambda" / "index.py"
PIPELINE = FUNCTIONS / "call_event_processor" / "event_processor" / "call_event_processor.py"
REGENERATOR = FUNCTIONS / "summary_regenerator" / "index.py"
SUMMARY_STATE_JS = AI_STACK / "source" / "ui" / "src" / "components" / "common" / "summary-state.js"


def _constant(path: Path, name: str) -> str:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name for target in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{name} is gone from {path.relative_to(AI_STACK)}")


def _js_constant(name: str) -> str:
    match = re.search(
        rf"export const {name} = '([^']*)';", SUMMARY_STATE_JS.read_text(encoding="utf-8")
    )
    assert match, f"{name} is gone from summary-state.js"
    return match.group(1)


@pytest.mark.parametrize("path", [PIPELINE, REGENERATOR])
def test_the_readers_recognise_the_summarisers_sentence(path: Path) -> None:
    assert _constant(path, "EMPTY_SUMMARY_TEXT") == _constant(SUMMARISER, "EMPTY_SUMMARY_TEXT")


def test_both_writers_and_the_meeting_page_name_the_status_alike() -> None:
    assert _constant(PIPELINE, "SUMMARY_STATUS_EMPTY") == "empty"
    assert _constant(REGENERATOR, "STATUS_EMPTY") == "empty"
    assert _js_constant("STATUS_EMPTY") == "empty"


def test_the_meeting_page_says_the_same_sentence() -> None:
    """The page's own wording, for a state written without a message."""
    sentence = _constant(SUMMARISER, "EMPTY_SUMMARY_TEXT")
    assert _js_constant("SUMMARY_EMPTY_TEXT").rstrip(".") == sentence
