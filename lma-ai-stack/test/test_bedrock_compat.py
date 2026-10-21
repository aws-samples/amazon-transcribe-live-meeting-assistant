# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Tests for bedrock_compat, the module that decides what each model generation gets.

Two things about a Bedrock Claude call vary by model, and both are silent or fatal
rather than merely degraded, which is why they are decided in one place and
asserted here rather than only at the call sites:

* ``temperature`` is rejected by Claude 5 and later. A request that carries it
  fails, so a stack deployed with one of those models would return an error for
  every summary, every parsed invitation and every speaker-naming pass.
* a reasoning-capable model may answer with a ``reasoningContent`` block ahead of
  the ``text`` block, and may spend its whole output budget on reasoning and
  return no text at all. Reading block zero either stores the model's working as
  the answer or raises, depending on the block's keys.

The module is duplicated verbatim into four deployment packages across two
CloudFormation stacks, because each is zipped on its own and there is no layer
they share. ``test_every_package_ships_the_same_compat_module`` is what keeps the
copies honest; the rest of the file tests the canonical one.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
LAMBDA_FUNCTIONS = REPO / "lma-ai-stack" / "source" / "lambda_functions"

# Every deployment package that ships a copy. The first is the canonical one the
# rest are compared against; it is also the one these tests import.
COMPAT_COPIES = (
    LAMBDA_FUNCTIONS / "bedrock_summary_lambda" / "bedrock_compat.py",
    LAMBDA_FUNCTIONS / "meeting_invitation_parser" / "bedrock_compat.py",
    LAMBDA_FUNCTIONS / "speaker_name_resolver" / "bedrock_compat.py",
    REPO / "lma-meetingassist-setup-stack" / "src" / "bedrock_compat.py",
)


@pytest.fixture(scope="module")
def compat():
    """The canonical copy, imported under a name of its own.

    Loaded by path rather than by ``import bedrock_compat`` so that whichever
    Lambda directory happens to be on sys.path cannot decide which copy is tested.
    """
    spec = importlib.util.spec_from_file_location("lma_bedrock_compat", COMPAT_COPIES[0])
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ── the duplication itself ──────────────────────────────────────────────────


def test_every_package_ships_the_same_compat_module() -> None:
    """Four packages carry this file; editing one and not the others is the risk.

    The three Lambda directories are zipped separately by SAM and the meeting
    assist copy belongs to a different CloudFormation stack, so none of them can
    import a shared one. Only this test would notice a divergence: each copy is
    exercised by its own package's tests, which would all still pass.
    """
    canonical = COMPAT_COPIES[0]
    for copy in COMPAT_COPIES[1:]:
        assert copy.exists(), f"{copy} is missing"
        assert copy.read_text() == canonical.read_text(), (
            f"{copy.relative_to(REPO)} has drifted from "
            f"{canonical.relative_to(REPO)}; copy the canonical file over it"
        )


def test_the_compat_module_has_no_third_party_imports(compat) -> None:
    """It has to import cleanly in four packages with four different requirements."""
    source = COMPAT_COPIES[0].read_text()
    imports = {
        line.split()[1].split(".")[0]
        for line in source.splitlines()
        if line.startswith("import ") or line.startswith("from ")
    }
    assert imports <= {"logging", "re", "__future__"}, f"unexpected imports: {imports}"


# ── which models take a temperature ─────────────────────────────────────────


@pytest.mark.parametrize(
    "model_id, major",
    [
        # Claude 3.x names the generation before the family.
        ("anthropic.claude-3-haiku-20240307-v1:0", 3),
        ("anthropic.claude-3-sonnet-20240229-v1:0", 3),
        ("us.anthropic.claude-3-5-sonnet-20241022-v2:0", 3),
        ("eu.anthropic.claude-3-5-sonnet-20240620-v1:0", 3),
        ("us.anthropic.claude-3-7-sonnet-20250219-v1:0", 3),
        # Claude 4+ names the family first.
        ("us.anthropic.claude-opus-4-20250514-v1:0", 4),
        ("us.anthropic.claude-opus-4-1-20250805-v1:0", 4),
        ("global.anthropic.claude-haiku-4-5-20251001-v1:0", 4),
        ("global.anthropic.claude-sonnet-4-5-20250929-v1:0", 4),
        ("us.anthropic.claude-sonnet-4-6", 4),
        ("global.anthropic.claude-opus-4-6-v1", 4),
        # Claude 5.5 carries neither a date stamp nor a version suffix.
        ("global.anthropic.claude-haiku-5-5", 5),
        ("us.anthropic.claude-haiku-5-5", 5),
        ("global.anthropic.claude-sonnet-5-5", 5),
        ("us.anthropic.claude-sonnet-5-5", 5),
        ("global.anthropic.claude-opus-5-5", 5),
        ("us.anthropic.claude-opus-5-5", 5),
        # Not a Claude model at all.
        ("amazon.nova-micro-v1:0", None),
        ("amazon.nova-pro-v1:0", None),
        ("us.amazon.nova-premier-v1:0", None),
        ("global.amazon.nova-2-lite-v1:0", None),
        ("amazon.nova-2-sonic-v1:0", None),
        ("", None),
    ],
)
def test_the_generation_is_read_from_either_naming_scheme(compat, model_id, major) -> None:
    """The family name moved relative to the version between Claude 3.x and 4.

    Both forms are still selectable, so one pattern has to read both. Getting this
    wrong in the permissive direction sends a temperature to a model that refuses
    it, which fails every request.
    """
    assert compat.claude_major_version(model_id) == major


@pytest.mark.parametrize(
    "model_id",
    [
        "anthropic.claude-3-haiku-20240307-v1:0",
        "global.anthropic.claude-haiku-4-5-20251001-v1:0",
        "us.anthropic.claude-sonnet-4-6",
        "amazon.nova-pro-v1:0",
        "global.amazon.nova-2-lite-v1:0",
    ],
)
def test_models_that_take_a_temperature_still_get_one(compat, model_id) -> None:
    """Determinism is not dropped for everybody to accommodate the new models.

    LMA relies on ``temperature: 0`` wherever two runs over one meeting should
    agree, and every model listed here still honours it.
    """
    assert compat.accepts_temperature(model_id) is True
    assert compat.build_inference_config(model_id, 4096) == {"maxTokens": 4096, "temperature": 0}


@pytest.mark.parametrize(
    "model_id",
    [
        "global.anthropic.claude-haiku-5-5",
        "us.anthropic.claude-haiku-5-5",
        "global.anthropic.claude-sonnet-5-5",
        "us.anthropic.claude-sonnet-5-5",
        "global.anthropic.claude-opus-5-5",
        "us.anthropic.claude-opus-5-5",
    ],
)
def test_models_that_refuse_a_temperature_are_not_sent_one(compat, model_id) -> None:
    """A request carrying it is rejected outright, not answered less precisely."""
    assert compat.accepts_temperature(model_id) is False
    assert compat.build_inference_config(model_id, 4096) == {"maxTokens": 4096}


def test_an_unrecognised_later_claude_generation_is_assumed_to_refuse_one(compat) -> None:
    """The two failure directions are not symmetrical, so the cutoff is open-ended.

    Sending a temperature to a model that refuses it fails every request; omitting
    it from one that would have accepted it only costs determinism. A Claude
    generation beyond anything this code knows about therefore gets the safe
    treatment without needing a code change first.
    """
    assert compat.accepts_temperature("global.anthropic.claude-haiku-9-1") is False


def test_the_temperature_passed_in_is_the_one_sent(compat) -> None:
    """The conversational paths use a non-zero temperature; it is not hard-coded."""
    config = compat.build_inference_config("amazon.nova-pro-v1:0", 500, 0.3)
    assert config == {"maxTokens": 500, "temperature": 0.3}


# ── reading the answer back ─────────────────────────────────────────────────


def test_a_single_text_block_is_read_as_the_answer(compat) -> None:
    response = {"output": {"message": {"content": [{"text": "hello"}]}}}
    assert compat.extract_text(response) == "hello"


def test_a_reasoning_block_before_the_text_is_not_part_of_the_answer(compat) -> None:
    """The shape a reasoning-capable model returns for a long prompt.

    The reasoning must not reach a stored summary or a response that gets parsed,
    and it is not reliably absent: the same model answers a short prompt with a
    lone text block.
    """
    response = {
        "output": {
            "message": {
                "content": [
                    {"reasoningContent": {"reasoningText": {"text": "working it out"}}},
                    {"text": "hello"},
                ]
            }
        },
        "stopReason": "end_turn",
    }
    assert compat.extract_text(response) == "hello"


def test_every_text_block_is_joined_in_order(compat) -> None:
    """The API does not promise exactly one text block, so position is no guide."""
    response = {
        "output": {
            "message": {
                "content": [
                    {"text": "first "},
                    {"reasoningContent": {"reasoningText": {"text": "ignored"}}},
                    {"text": "second"},
                ]
            }
        }
    }
    assert compat.extract_text(response) == "first second"


def test_a_response_of_nothing_but_reasoning_raises(compat) -> None:
    """Reasoning tokens are billed against maxTokens and come before any text.

    Measured on a real transcript, a reasoning model given the budget this code
    used to send spent all of it reasoning and returned no text. Raising is what
    stops a blank string overwriting a good summary; the error names the stop
    reason so the log says why.
    """
    response = {
        "output": {
            "message": {"content": [{"reasoningContent": {"reasoningText": {"text": "..."}}}]}
        },
        "stopReason": "max_tokens",
    }
    with pytest.raises(ValueError, match="no text block"):
        compat.extract_text(response)


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"output": {}},
        {"output": {"message": {}}},
        {"output": {"message": {"content": []}}},
        {"output": {"message": {"content": [{}]}}},
        {"output": {"message": {"content": "not a list"}}},
    ],
    ids=["empty", "no-message", "no-content", "no-blocks", "block-without-text", "not-a-list"],
)
def test_a_response_of_an_unexpected_shape_raises_rather_than_returning_empty(
    compat, response
) -> None:
    """No coerced reading of a response the model did not produce."""
    with pytest.raises(ValueError):
        compat.extract_text(response)


def test_hitting_the_token_ceiling_is_logged_even_when_there_is_text(compat, caplog) -> None:
    """A truncated answer is kept -- a mostly-complete summary beats none -- but said.

    Nothing else in the response distinguishes a complete answer from one cut off
    at the budget, so this warning is the only signal that a call site's budget
    needs raising.
    """
    response = {
        "output": {"message": {"content": [{"text": "half a sum"}]}},
        "stopReason": "max_tokens",
    }
    with caplog.at_level("WARNING"):
        assert compat.extract_text(response) == "half a sum"
    assert "maxTokens ceiling" in caplog.text


def test_a_complete_answer_is_not_logged_as_truncated(compat, caplog) -> None:
    response = {
        "output": {"message": {"content": [{"text": "a whole summary"}]}},
        "stopReason": "end_turn",
    }
    with caplog.at_level("WARNING"):
        compat.extract_text(response)
    assert "maxTokens ceiling" not in caplog.text


# ── the budgets ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "name",
    [
        "SUMMARY_MAX_TOKENS",
        "INVITATION_PARSER_MAX_TOKENS",
        "SPEAKER_NAMING_MAX_TOKENS",
        "MEETING_ASSIST_FALLBACK_MAX_TOKENS",
    ],
)
def test_every_budget_leaves_room_for_reasoning(compat, name) -> None:
    """A reasoning model was measured spending more than 2,048 on one answer.

    It hit the ceiling it was given, so the true requirement is above that figure
    rather than at it. Raising these costs nothing on the models that do not
    reason, because maxTokens is a ceiling and not a reservation.
    """
    assert getattr(compat, name) > 2048
