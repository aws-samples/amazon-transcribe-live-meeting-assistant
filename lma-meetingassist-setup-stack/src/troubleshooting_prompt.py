#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""Read the troubleshooting agent's system prompt from DynamoDB.

WHY THE PROMPT IS NOT A PYTHON STRING
-------------------------------------
The quality of this prompt is the whole feature. It names LMA's failure
patterns, the three things a naive reader gets wrong about the virtual
participant's persisted state, and the cases where an alarming log line is the
system working — and every one of those will need correcting in the field as
LMA changes and as the agent is seen to reason badly. A prompt that can only be
changed by a code change and a 35-minute stack deployment will not be tuned,
and an untuned prompt is the failure mode this feature has.

So it lives in ``LLMPromptTemplateTable``, in the Default/Custom pair LMA
already uses for its summarization prompts:

* ``DefaultTroubleshootingAgentPrompt`` is written by the deployment, and
  rewritten whenever a stack update ships a changed prompt — the seeding
  custom resource re-runs when the content hash over its source changes.
  Operator edits to it are lost, by design.
* ``CustomTroubleshootingAgentPrompt`` is seeded empty under a condition that
  can only create it, so it appears on an upgraded stack as well as a new one
  and is never rewritten once it exists. An operator who puts a
  ``systemPrompt`` attribute here overrides the default, and the override
  survives upgrades.

This is the interim home named in the design: when the unified configuration
store lands it becomes one more item to migrate, which is the same cost as the
four config stores already in the tree, and strictly less than standing up a
fifth bespoke one now.

WHY A MISSING PROMPT IS A HARD FAILURE
--------------------------------------
There is deliberately no fallback string in this module. An agent that
diagnoses LMA without the "what is and is not persisted" briefing does not
degrade gracefully — it reads an empty log group as evidence and reports a
cause that is not there. A diagnosis produced from an unknown prompt is worse
than no diagnosis, so an unreadable prompt raises.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

_LOGGER = logging.getLogger(__name__)

DEFAULT_PROMPT_PK = "DefaultTroubleshootingAgentPrompt"
CUSTOM_PROMPT_PK = "CustomTroubleshootingAgentPrompt"

#: The attribute holding the prompt text on both items.
PROMPT_ATTRIBUTE = "systemPrompt"

#: Written in place of a value to mean "fall back to the default", matching the
#: convention the summarization prompt templates already use.
DISABLED_SENTINEL = "NONE"

#: Below this, the stored value is not a system prompt — it is a truncated
#: paste, an empty-string placeholder, or a note someone left in the field.
#: Falling back to the default is better than running on a fragment.
_MINIMUM_USEFUL_LENGTH = 200


class PromptUnavailableError(RuntimeError):
    """The system prompt could not be read, so the agent must not run."""


def _read_prompt_attribute(
    item: Optional[Dict[str, Any]], source: str = ""
) -> Optional[str]:
    """Pull the prompt text out of one DynamoDB item, or return None.

    ``<br>`` is translated to a newline. The stored default never contains one
    — it is written verbatim from a text file — but an operator editing the
    custom item in the DynamoDB console has no good way to enter a newline, and
    the summarization templates in the same table already use this convention.
    Supporting it here means the override mechanism works from the console
    without a second tool.

    Every reason for rejecting a value is logged when ``source`` names one, for
    the same reason the limits reader logs a clamp: an operator whose override
    is being ignored has no other way to find out, and the symptom — an agent
    behaving as if the override were not there — is indistinguishable from the
    override not having been saved.
    """
    if not item:
        return None
    attribute = item.get(PROMPT_ATTRIBUTE)
    if not isinstance(attribute, dict):
        if source and PROMPT_ATTRIBUTE in item:
            _LOGGER.warning(
                "%s has a '%s' attribute that is not a string; ignoring it",
                source,
                PROMPT_ATTRIBUTE,
            )
        return None
    text = attribute.get("S")
    if not isinstance(text, str):
        return None
    text = text.replace("<br>", "\n").strip()
    if not text:
        return None
    if text == DISABLED_SENTINEL:
        if source:
            _LOGGER.info("%s is set to %s; using the default", source, DISABLED_SENTINEL)
        return None
    if len(text) < _MINIMUM_USEFUL_LENGTH:
        if source:
            _LOGGER.warning(
                "%s has a '%s' of only %d characters, which is too short to be a "
                "system prompt; ignoring it and using the default. Set at least "
                "%d characters, or clear it to use the default deliberately.",
                source,
                PROMPT_ATTRIBUTE,
                len(text),
                _MINIMUM_USEFUL_LENGTH,
            )
        return None
    return text


def get_system_prompt(dynamodb_client: Any, table_name: str) -> str:
    """Return the system prompt, preferring the operator's override.

    Args:
        dynamodb_client: A low-level DynamoDB client.
        table_name: The LLMPromptTemplateTable name.

    Returns:
        The prompt text.

    Raises:
        PromptUnavailableError: if neither item yields a usable prompt, or the
            table cannot be read. The caller must not substitute one.
    """
    if not table_name:
        raise PromptUnavailableError(
            "the prompt template table is not configured for this function"
        )

    # Two reads rather than one BatchGetItem, and the override is read first so
    # the default is not fetched when it is not needed.
    #
    # An unreadable override — including a throttle — falls back to the shipped
    # default with a warning, rather than failing the run. That is the right
    # trade for this one field: the default is a complete, current prompt, so a
    # diagnosis run on it is a good diagnosis, just not the operator's tuned
    # one. The read that must succeed is the default's, below, and that one
    # raises.
    try:
        custom = dynamodb_client.get_item(
            TableName=table_name,
            Key={"LLMPromptTemplateId": {"S": CUSTOM_PROMPT_PK}},
        ).get("Item")
    except Exception:  # noqa: BLE001 - an unreadable override is not fatal
        # Deliberately not re-raised: an operator override that cannot be read
        # is a reason to fall back to the shipped default, not a reason to
        # refuse to diagnose. The default read below is the one that must
        # succeed.
        _LOGGER.warning(
            "could not read %s; falling back to %s", CUSTOM_PROMPT_PK, DEFAULT_PROMPT_PK
        )
        custom = None

    override = _read_prompt_attribute(custom, source=CUSTOM_PROMPT_PK)
    if override:
        return override

    try:
        default = dynamodb_client.get_item(
            TableName=table_name,
            Key={"LLMPromptTemplateId": {"S": DEFAULT_PROMPT_PK}},
        ).get("Item")
    except Exception as exc:  # noqa: BLE001 - re-raised as the module's own error
        raise PromptUnavailableError(
            f"could not read {DEFAULT_PROMPT_PK} from {table_name}: {exc}"
        ) from exc

    shipped = _read_prompt_attribute(default, source=DEFAULT_PROMPT_PK)
    if shipped:
        return shipped

    raise PromptUnavailableError(
        f"neither {CUSTOM_PROMPT_PK} nor {DEFAULT_PROMPT_PK} in {table_name} "
        f"holds a usable '{PROMPT_ATTRIBUTE}' attribute. The default is written "
        "by the LLM template stack's prompt-seeding custom resource, which runs "
        "when that stack's source hash changes — so republishing it, or any "
        "update that changes its source, restores the default."
    )
