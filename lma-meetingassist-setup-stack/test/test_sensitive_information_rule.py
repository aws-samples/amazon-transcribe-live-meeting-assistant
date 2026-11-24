#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""Meeting Assistant › Leave out sensitive personal information.

``meeting_assist.omitSensitivePersonalInformation`` (off by default; the
Minimised content preset turns it on) appends ``SENSITIVE_INFORMATION_RULE``
to the fixed part of the system prompt, after whatever the profile's Assistant
instructions say, on every kind of request. Off, the prompt is unchanged. The
rule names the same categories as the minimised-content summary profile's
sections, so the summary and the assistant leave out the same things.

The handler is driven end to end with the stand-ins of
test_in_meeting_request_scope.py, so these assert what the Agent is given.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import strands_meeting_assist_function as handler_module
from test_in_meeting_request_scope import (  # noqa: F401 -- pytest fixtures
    IN_MEETING,
    built,
    configured,
)

REPO = Path(__file__).resolve().parents[2]
SUMMARY_PROFILES = (
    REPO
    / "lma-ai-stack/source/lambda_functions/configuration_seeder/defaults/SummaryProfiles.json"
)

#: The categories, as the rule and the summary profile both name them.
CATEGORIES = (
    "health and medical information (conditions, symptoms, diagnoses, treatments, medication, "
    "disabilities, pregnancy and mental health), racial or ethnic origin, religious or "
    "philosophical beliefs, political opinions, trade union membership, sex life and sexual "
    "orientation, gender identity, genetic and biometric data, and criminal convictions or "
    "allegations"
)

ADMIN_PROMPT = "You are Pat, the team's meeting helper. Answer in one sentence."

EVENTS = [{"userEmail": "owner@example.com"}, *IN_MEETING]
EVENT_IDS = ["meeting-page", "chat", "voice", "wake-phrase"]


@pytest.mark.parametrize("event", EVENTS, ids=EVENT_IDS)
def test_the_rule_follows_the_fixed_part_when_the_setting_is_on(built, configured, event):
    configured({"omitSensitivePersonalInformation": True})
    prompt = built(event)["system_prompt"]
    assert handler_module.FIXED_INSTRUCTIONS + handler_module.SENSITIVE_INFORMATION_RULE in prompt


@pytest.mark.parametrize("event", EVENTS, ids=EVENT_IDS)
def test_an_admins_own_instructions_are_kept_with_the_rule_after_them(built, configured, event):
    configured({"systemPrompt": ADMIN_PROMPT, "omitSensitivePersonalInformation": True})
    prompt = built(event)["system_prompt"]
    assert prompt.startswith(ADMIN_PROMPT)
    assert prompt.index(ADMIN_PROMPT) < prompt.index(handler_module.SENSITIVE_INFORMATION_RULE)


@pytest.mark.parametrize("value", [False, None, "off"])
def test_the_rule_is_absent_unless_the_setting_is_on(built, configured, value):
    configured({} if value is None else {"omitSensitivePersonalInformation": value})
    prompt = built({"userEmail": "owner@example.com"})["system_prompt"]
    assert handler_module.SENSITIVE_INFORMATION_RULE not in prompt
    assert "sensitive personal information" not in prompt


def test_the_rule_names_the_categories_the_summary_profile_names():
    assert CATEGORIES in handler_module.SENSITIVE_INFORMATION_RULE
    profile = json.loads(SUMMARY_PROFILES.read_text(encoding="utf-8"))["minimised-content"]
    for title, prompt in profile["prompts"].items():
        assert CATEGORIES in prompt, title


def test_the_setting_is_off_in_the_shipped_defaults():
    defaults = json.loads(
        (
            REPO
            / "lma-ai-stack/source/lambda_layers/configuration_layer/lma_config/setting_defaults.json"
        ).read_text(encoding="utf-8")
    )
    assert defaults["meeting_assist"]["omitSensitivePersonalInformation"] == {
        "type": "boolean",
        "default": False,
    }
