#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Model-shape differences between Bedrock Claude generations, decided in one place.

Every LMA Converse call used to be written the same way: an ``inferenceConfig`` of
``{"maxTokens": N, "temperature": 0}`` and a response read as
``content[0]["text"]``. That shape is specific to the models that were selectable
when it was written. Reasoning-capable Claude generations (Claude 5 and later)
differ from it in three ways, so all three are settled here rather than repeated
at each call site:

``build_inference_config``
    Claude 5 and later do not accept ``temperature`` in ``inferenceConfig``; a
    request carrying it is rejected outright. The Nova models and Claude 4.x and
    earlier do accept it, and LMA depends on ``temperature: 0`` wherever two runs
    over one meeting should agree (summaries, speaker-name attribution). So
    temperature is sent to the models that take it and omitted for the ones that
    do not, rather than dropped for everybody.

``extract_text``
    A reasoning-capable model may return a ``reasoningContent`` block ahead of the
    ``text`` block, and whether it does is prompt-dependent: the same model
    answers a short prompt with a single text block and a long transcript with
    reasoning first. Reading block zero therefore works right up until it does
    not. Every ``text`` block is concatenated in document order and nothing else
    is read, so the model's reasoning never reaches a stored summary or a
    response that gets parsed downstream.

``inference budgets``
    Reasoning tokens are billed against ``maxTokens`` and are produced before any
    text, so a budget sized for a non-reasoning model can be spent entirely on
    reasoning and yield an empty answer with ``stopReason == "max_tokens"``.
    ``extract_text`` logs that stop reason and refuses a response with no text at
    all, so a truncated call is visible in the log and an empty one raises
    instead of overwriting a good result with a blank. The budgets themselves are
    the call sites' to choose; see ``SUMMARY_MAX_TOKENS`` and friends below for
    the sizes and the measurements behind them.

This file is duplicated **verbatim** into every deployment package that needs it:
three Lambda directories under ``lma-ai-stack/source/lambda_functions/`` and the
meeting-assist function's ``src`` directory in ``lma-meetingassist-setup-stack``.
Each of those is zipped on its own and they belong to different CloudFormation
stacks, so there is no layer they can share. ``lma-ai-stack/test/
test_bedrock_compat.py`` asserts the copies are byte-identical, the same way the
Virtual Participant and ASR stacks guard their shared ``microvm_client.py``.
"""

from __future__ import annotations

import logging
import re

LOGGER = logging.getLogger(__name__)

# Output budgets, in tokens. Shared here so the reasoning they are sized by stays
# attached to the numbers.
#
# The reasoning overhead is what sets these, not the length of the answer. On a
# 4,400-word meeting transcript with the shipped summary prompt, Claude Haiku 4.5
# produced 330-577 output tokens; the reasoning-capable models asked the same
# question produced 1,148-1,507 (Sonnet 5.5) and 1,273-2,048+ (Haiku 5.5), where
# the largest figure is the budget it was given rather than what it wanted - it
# stopped at the ceiling. The true requirement for the longest transcript measured
# is therefore unknown and above 2,048.
#
# 4,096 is double the largest consumption observed and roughly seven times what a
# non-reasoning model uses for the same job. It is affordable because maxTokens is
# a ceiling, not a reservation: a model that answers in 400 tokens is billed for
# 400 whichever budget it was offered, so raising the ceiling costs nothing on the
# models that do not reason. Note 577 > 512, so the previous summary budget was
# already clipping the longest answers Haiku 4.5 wanted to give.
#
# Only the summary figure is measured. The other two are sized by analogy: the
# speaker-name resolver sends up to 160 transcript turns, a prompt of the same
# order as the summary's, and the invitation parser sends a single invitation body
# and so needs less, but a common ceiling costs nothing and leaves one number to
# reason about.
SUMMARY_MAX_TOKENS = 4096
INVITATION_PARSER_MAX_TOKENS = 4096
SPEAKER_NAMING_MAX_TOKENS = 4096
MEETING_ASSIST_FALLBACK_MAX_TOKENS = 4096

# The Converse stopReason that means the answer was cut off at the ceiling.
TRUNCATED_STOP_REASON = "max_tokens"

# First Claude major generation that rejects ``temperature``.
_TEMPERATURE_DROPPED_AT_MAJOR = 5

# Claude ids carry the generation on either side of the family name depending on
# vintage: "claude-3-5-sonnet-20240620-v1:0" puts it first, while
# "claude-haiku-4-5-20251001-v1:0" and "claude-haiku-5-5" put the family first.
# The optional letters cannot swallow the digits, so one pattern reads both.
_CLAUDE_MAJOR = re.compile(r"claude-(?:[a-z]+-)?(\d+)")


def claude_major_version(model_id: str) -> int | None:
    """The major Claude generation named in ``model_id``, or None if it is not Claude.

    Regional and global inference-profile prefixes ("us.", "global.", "eu.",
    "apac.") are ignored, since they do not change the model's behaviour.
    """
    if not model_id or "anthropic." not in model_id:
        return None
    match = _CLAUDE_MAJOR.search(model_id)
    return int(match.group(1)) if match else None


def accepts_temperature(model_id: str) -> bool:
    """Whether ``model_id`` takes ``temperature`` in ``inferenceConfig``.

    Decided from the generation number rather than from a list of exact model ids,
    so a model added to a CloudFormation ``AllowedValues`` list later is handled
    without a matching code change. The two failure directions are not
    symmetrical, which is what settles the treatment of an id this does not
    recognise: sending temperature to a model that refuses it fails every single
    request, while omitting it from a model that would have accepted it only
    costs determinism. So any Claude generation at or above the cutoff is treated
    as refusing it, and anything that is not a Claude model at all keeps it.
    """
    major = claude_major_version(model_id)
    if major is None:
        return True
    return major < _TEMPERATURE_DROPPED_AT_MAJOR


def build_inference_config(
    model_id: str, max_tokens: int, temperature: float = 0
) -> dict[str, object]:
    """The ``inferenceConfig`` for a Converse call against ``model_id``."""
    config: dict[str, object] = {"maxTokens": max_tokens}
    if accepts_temperature(model_id):
        config["temperature"] = temperature
    return config


def extract_text(response: dict) -> str:
    """The model's answer from a Converse response, reasoning blocks excluded.

    Raises ``ValueError`` when the response carries no text block at all. That is
    the shape a reasoning model returns when the whole budget went on reasoning,
    and raising is what stops a blank string being stored as a meeting summary or
    parsed as if the model had answered. The caller's existing error handling then
    reports it the same way it reports a throttled or failed call.
    """
    if response.get("stopReason") == TRUNCATED_STOP_REASON:
        LOGGER.warning(
            "Bedrock stopped at the maxTokens ceiling: the answer is truncated and may be "
            "empty, because reasoning tokens are billed against the same budget. Raise the "
            "budget passed to build_inference_config for this call site."
        )

    blocks = response.get("output", {}).get("message", {}).get("content", [])
    if not isinstance(blocks, list):
        blocks = []
    texts = [
        block["text"]
        for block in blocks
        if isinstance(block, dict) and isinstance(block.get("text"), str)
    ]
    if not texts:
        kinds = sorted({key for block in blocks if isinstance(block, dict) for key in block})
        raise ValueError(
            f"Bedrock response carried no text block (blocks present: {kinds or 'none'}, "
            f"stopReason: {response.get('stopReason')!r})"
        )
    return "".join(texts)
