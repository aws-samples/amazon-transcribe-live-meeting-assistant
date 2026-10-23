#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""Unit tests for reading the troubleshooting system prompt (no AWS)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import troubleshooting_prompt as prompts
from troubleshooting_prompt import PromptUnavailableError, get_system_prompt

TABLE = "LMA-Test-LLMPromptTemplateTable"

# Long enough to pass the minimum-length check, so a test about precedence is
# not accidentally a test about length.
DEFAULT_TEXT = "DEFAULT PROMPT. " * 40
CUSTOM_TEXT = "CUSTOM PROMPT. " * 40

#: The seed file the stack writes into the default item.
PROMPT_FILE = (
    Path(__file__).resolve().parents[2]
    / "lma-llm-template-setup-stack"
    / "source"
    / "lambda_functions"
    / "TroubleshootingAgentSystemPrompt.md"
)


class FakeDynamoDBClient:
    """Answers get_item from a {partition key: item} map."""

    def __init__(self, items: Optional[Dict[str, Any]] = None, raises: Any = None):
        self.items = items or {}
        self.raises = raises or {}
        self.keys_requested: List[str] = []

    def get_item(self, TableName: str, Key: Dict[str, Any]) -> Dict[str, Any]:  # noqa: N803
        assert TableName == TABLE
        key = Key["LLMPromptTemplateId"]["S"]
        self.keys_requested.append(key)
        if key in self.raises:
            raise self.raises[key]
        item = self.items.get(key)
        return {"Item": item} if item else {}


def item(text: Optional[str] = None, **extra: Any) -> Dict[str, Any]:
    attributes: Dict[str, Any] = {"*Information*": {"S": "notes"}}
    if text is not None:
        attributes["systemPrompt"] = {"S": text}
    attributes.update(extra)
    return attributes


def both(default: Optional[str] = DEFAULT_TEXT, custom: Optional[str] = None) -> FakeDynamoDBClient:
    items: Dict[str, Any] = {}
    if default is not None:
        items[prompts.DEFAULT_PROMPT_PK] = item(default)
    if custom is not None:
        items[prompts.CUSTOM_PROMPT_PK] = item(custom)
    else:
        # The custom item exists but carries no prompt, which is how the stack
        # seeds it on create.
        items[prompts.CUSTOM_PROMPT_PK] = item()
    return FakeDynamoDBClient(items)


# ── precedence ──────────────────────────────────────────────────────────────


def test_the_shipped_default_is_used_when_nothing_overrides_it() -> None:
    assert get_system_prompt(both(), TABLE) == DEFAULT_TEXT.strip()


def test_an_operator_override_replaces_the_default() -> None:
    """Tuning must not need a redeployment: the prompt's wording is the whole
    feature, and a 35-minute stack update per iteration means it is not tuned.
    """
    assert get_system_prompt(both(custom=CUSTOM_TEXT), TABLE) == CUSTOM_TEXT.strip()


def test_the_default_is_not_even_read_when_an_override_is_present() -> None:
    client = both(custom=CUSTOM_TEXT)

    get_system_prompt(client, TABLE)

    assert prompts.DEFAULT_PROMPT_PK not in client.keys_requested


def test_an_absent_custom_item_falls_back_to_the_default() -> None:
    client = FakeDynamoDBClient({prompts.DEFAULT_PROMPT_PK: item(DEFAULT_TEXT)})

    assert get_system_prompt(client, TABLE) == DEFAULT_TEXT.strip()


@pytest.mark.parametrize("override", ["", "   ", "NONE", "\n\n"])
def test_a_blanked_override_falls_back_rather_than_running_on_nothing(
    override: str,
) -> None:
    """'NONE' is how the sibling prompt templates in this table are disabled."""
    assert get_system_prompt(both(custom=override), TABLE) == DEFAULT_TEXT.strip()


def test_a_fragment_of_a_prompt_is_not_used_as_one() -> None:
    """A truncated paste or a note someone left in the field is not a prompt,
    and falling back to the default beats running on a fragment."""
    assert get_system_prompt(both(custom="be helpful"), TABLE) == DEFAULT_TEXT.strip()


def test_a_br_encoded_override_is_turned_back_into_lines() -> None:
    """An operator editing this item in the DynamoDB console has no good way to
    enter a newline, and the sibling templates in the same table already use
    this encoding."""
    encoded = "<br>".join(["line one"] * 30)

    result = get_system_prompt(both(custom=encoded), TABLE)

    assert "<br>" not in result
    assert result.count("\n") == 29


# ── failure is loud ─────────────────────────────────────────────────────────


def test_a_table_with_neither_item_refuses_to_produce_a_prompt() -> None:
    """There is deliberately no fallback string anywhere in this module.

    An agent run without the "what is and is not persisted" briefing does not
    degrade gracefully: it reads an empty log group as evidence and reports a
    cause that is not there. A diagnosis from an unknown prompt is worse than
    no diagnosis.
    """
    with pytest.raises(PromptUnavailableError, match="usable 'systemPrompt'"):
        get_system_prompt(FakeDynamoDBClient({}), TABLE)


def test_the_refusal_says_how_to_restore_the_default() -> None:
    with pytest.raises(PromptUnavailableError, match="stack update will restore it"):
        get_system_prompt(FakeDynamoDBClient({}), TABLE)


def test_an_unconfigured_table_refuses_rather_than_improvising() -> None:
    with pytest.raises(PromptUnavailableError, match="not configured"):
        get_system_prompt(FakeDynamoDBClient(), "")


def test_a_failure_reading_the_default_is_raised_as_this_modules_own_error() -> None:
    client = FakeDynamoDBClient(raises={prompts.DEFAULT_PROMPT_PK: RuntimeError("throttled")})

    with pytest.raises(PromptUnavailableError, match="throttled"):
        get_system_prompt(client, TABLE)


def test_a_failure_reading_the_override_still_yields_the_default() -> None:
    """An unreadable override is not a reason to refuse to diagnose."""
    client = FakeDynamoDBClient(
        {prompts.DEFAULT_PROMPT_PK: item(DEFAULT_TEXT)},
        raises={prompts.CUSTOM_PROMPT_PK: RuntimeError("throttled")},
    )

    assert get_system_prompt(client, TABLE) == DEFAULT_TEXT.strip()


def test_a_prompt_attribute_of_the_wrong_type_is_not_used() -> None:
    client = FakeDynamoDBClient(
        {
            prompts.DEFAULT_PROMPT_PK: item(DEFAULT_TEXT),
            prompts.CUSTOM_PROMPT_PK: {"systemPrompt": {"N": "42"}},
        }
    )

    assert get_system_prompt(client, TABLE) == DEFAULT_TEXT.strip()


# ── the shipped prompt itself ───────────────────────────────────────────────
#
# Pinned loosely. The wording is meant to be edited; these assert the handful
# of things the agent is wrong without, each of which has a matching rule in
# the tools' own output.


def test_the_shipped_prompt_file_exists_where_the_seeder_reads_it() -> None:
    assert PROMPT_FILE.is_file()


def test_the_shipped_prompt_would_pass_this_modules_own_checks() -> None:
    text = PROMPT_FILE.read_text(encoding="utf-8")
    client = FakeDynamoDBClient({prompts.DEFAULT_PROMPT_PK: item(text)})

    assert get_system_prompt(client, TABLE) == text.strip()


def prompt_text() -> str:
    """The shipped prompt with its line wrapping flattened.

    Flattened deliberately: these assertions are about the rules the prompt
    states, and a reflow that moves a phrase onto the next line must not fail
    a test about its meaning.
    """
    return " ".join(PROMPT_FILE.read_text(encoding="utf-8").split())


@pytest.mark.parametrize(
    "required",
    [
        # The output contract the three sections rest on.
        "## Root Cause",
        "## Recommendations",
        "Evidence",
        "Not determined",
        # The constraint that shapes every investigation in LMA.
        "log-driven, not state-driven",
        # The two anti-patterns that make this agent usable rather than
        # confidently wrong.
        "NEVER CONSTRUCT A LOG GROUP NAME",
        "VPLaunchType",
        # The normal-behaviour cases a naive reader reports as causes.
        "falling back to Amazon Transcribe",
        "batch item failures",
        # The disclosure restriction. This output may be pasted into a public
        # issue tracker, so the agent is told to describe a defect as observed
        # behaviour rather than characterising it in security terms.
        "REPORTING RESTRICTIONS",
        "Do not characterise it in security terms",
        "security reporting process",
    ],
)
def test_the_shipped_prompt_carries_the_rules_the_agent_is_wrong_without(
    required: str,
) -> None:
    assert required in prompt_text()


@pytest.mark.parametrize(
    "marker",
    [
        # lma-virtual-participant-stack/backend/src/index.ts
        "VP exit:",
        "trigger=",
        # lma-websocket-transcriber-stack/source/app/src/
        "[TRANSCRIBING]",
        "[AUTH]",
        "[ASR]",
        "[DIARIZATION]",
        # lma-ai-stack/source/lambda_functions/call_event_processor/
        "reporting Kinesis batch item failures",
        "skipped Kinesis records that could not be decoded",
        # lma-virtual-participant-stack/template.yaml state machine states
        "MarkWaitingForCapacity",
    ],
)
def test_the_shipped_prompt_names_a_log_marker_the_code_actually_emits(
    marker: str,
) -> None:
    """A prompt that teaches a marker the code does not log sends the agent
    looking for a line that cannot exist, and an empty result from a search
    for it reads exactly like an absence of evidence.

    Each marker here is a literal present in LMA's own source; the comments
    above name the file it comes from.
    """
    assert marker in prompt_text()
