#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""The meeting assistant's instructions and per-item tool selection in the Meeting Assistant section.

``systemPrompt`` is the editable part of the assistant's system prompt, and
``toolSelection`` names installed MCP servers (and, later, skills) by id, each
with the flags ``enabled``, ``inMeetingRequests`` and ``alwaysOn``. Both are
ordinary settings of the section: shipped in ``setting_defaults.json``, checked
by the section schema on every save, carried by every Managed profile and moved
by export and import with the rest of the section.
"""

from __future__ import annotations

import pytest

from lma_config import errors, parameters, presets, schema
from lma_config.store import full_section


def _check(values):
    return schema.check(schema.section_schema("meeting_assist"), values, ("meeting_assist",))


def _shipped():
    return parameters.shipped_setting_defaults()["meeting_assist"]


def test_the_shipped_instructions_and_selection_are_valid():
    shipped = _shipped()
    assert isinstance(shipped["systemPrompt"], str) and shipped["systemPrompt"].strip()
    assert shipped["toolSelection"] == {"mcpServers": {}, "skills": {}}
    assert _check(shipped) == []


def test_a_server_entry_with_the_three_flags_is_accepted():
    values = {
        "toolSelection": {
            "mcpServers": {
                "@scope/server-a": {"enabled": True, "inMeetingRequests": False, "alwaysOn": False},
                "https://mcp.example.com/mcp": {"inMeetingRequests": True},
                "plain-server": {},
            },
            "skills": {"notes": {"enabled": False}},
        }
    }
    assert _check(values) == []


@pytest.mark.parametrize(
    "values, fragment",
    [
        ({"toolSelection": {"mcpServers": {"a": {"enabled": "yes"}}}}, "true or false"),
        ({"toolSelection": {"mcpServers": {"a": {"always": True}}}}, "always"),
        ({"toolSelection": {"agents": {}}}, "agents"),
        ({"toolSelection": {"mcpServers": {"bad\nid": {}}}}, "bad"),
        ({"toolSelection": {"mcpServers": []}}, "an object"),
        ({"systemPrompt": ""}, "systemPrompt"),
        ({"systemPrompt": "x" * 20001}, "systemPrompt"),
        ({"systemPrompt": 3}, "text"),
    ],
)
def test_a_malformed_value_is_refused_at_the_save(values, fragment):
    problems = _check(values)
    assert problems, values
    assert any(fragment in problem for problem in problems), problems


def test_the_store_refuses_a_malformed_selection():
    with pytest.raises(errors.InvalidConfiguration):
        schema.validate_section(
            "meeting_assist", {"toolSelection": {"mcpServers": {"a": {"enabled": 1}}}}
        )


def test_a_profile_entry_is_merged_over_the_empty_shipped_selection():
    merged = full_section(
        "meeting_assist",
        {"toolSelection": {"mcpServers": {"a": {"enabled": False}}}},
        _shipped(),
    )
    assert merged["toolSelection"] == {"mcpServers": {"a": {"enabled": False}}, "skills": {}}
    assert merged["systemPrompt"] == _shipped()["systemPrompt"]


def test_the_shipped_object_default_is_a_fresh_copy_for_each_caller():
    first = parameters.shipped_setting_defaults()["meeting_assist"]["toolSelection"]
    first["mcpServers"]["changed"] = {}
    assert parameters.shipped_setting_defaults()["meeting_assist"]["toolSelection"] == {
        "mcpServers": {},
        "skills": {},
    }


def test_an_object_setting_is_read_only_from_an_object():
    assert parameters.coerce("object", {"a": 1}) == {"a": 1}
    for raw in ("{}", "", 1, None, []):
        with pytest.raises(ValueError):
            parameters.coerce("object", raw)


@pytest.mark.parametrize("preset", presets.shipped_presets(), ids=lambda p: p["Id"])
def test_every_managed_preset_carries_the_shipped_instructions(preset):
    # A Managed profile is its preset applied over the baseline.
    managed = presets.apply_preset({"meeting_assist": _shipped()}, preset)
    assert managed["meeting_assist"]["systemPrompt"] == _shipped()["systemPrompt"]
    assert managed["meeting_assist"]["toolSelection"] == {"mcpServers": {}, "skills": {}}
