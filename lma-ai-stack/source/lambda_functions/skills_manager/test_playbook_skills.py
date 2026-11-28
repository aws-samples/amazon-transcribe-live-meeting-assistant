#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""A skill with a playbook section (docs/meeting-playbooks.md), as the Skills Manager keeps it.

The section is front matter, kept in the skill's metadata like the other
front-matter fields: read from SKILL.md, checked by the playbook rules on every
create, update and import (a template or a check the release does not know is
refused, naming what it accepts), stored normalised, and written back unchanged
on export, so a SKILL.md or a library round-trips.
"""

from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import skill_formats as sf  # noqa: E402

PLAYBOOK_MD = """---
name: launch-review
display_name: Launch review
description: Playbook for a launch review. Use when asked about the launch plan.
playbook:
  suggestFor: [launch review, go/no-go]
  whiteboards:
    - template: risks
      title: Launch risks
  guidance:
    checks: [risk_probe, action_missing_owner]
    assertiveness: quiet
    maxItemsPer10Min: 4
  quickActions:
    - label: Go or no-go
      prompt: Summarise the arguments for and against launching now.
---

# Launch review

Check the plan against the checklist.
"""


def test_a_playbook_section_is_kept_normalised_in_the_metadata():
    parsed = sf.parse_skill_file("SKILL.md", PLAYBOOK_MD.encode())
    assert parsed.notes == []
    playbook = parsed.skill.metadata["playbook"]
    assert playbook["whiteboards"] == [
        {"template": "risks", "title": "Launch risks", "instructions": "", "assistantUpdates": True}
    ]
    assert playbook["guidance"] == {
        "checks": ["risk_probe", "action_missing_owner"],
        "assertiveness": "quiet",
        "maxItemsPer10Min": 4,
    }
    assert playbook["suggestFor"] == ["launch review", "go/no-go"]


def test_the_section_round_trips_through_skill_md():
    skill = sf.parse_skill_file("SKILL.md", PLAYBOOK_MD.encode()).skill
    record = skill.to_item_fields()
    written = sf.render_skill_md(record)
    again = sf.parse_skill_file("SKILL.md", written.encode()).skill
    assert again.metadata == skill.metadata
    assert again.instructions == skill.instructions


def test_a_stored_playbook_read_back_with_decimals_exports_as_numbers():
    skill = sf.parse_skill_file("SKILL.md", PLAYBOOK_MD.encode()).skill
    record = skill.to_item_fields()
    record["Metadata"]["playbook"]["guidance"]["maxItemsPer10Min"] = Decimal(4)
    plain = sf.plain_item(record)
    assert plain["Metadata"]["playbook"]["guidance"]["maxItemsPer10Min"] == 4
    assert "maxItemsPer10Min: 4" in sf.render_skill_md(record)
    json.dumps(plain)  # nothing a JSON answer cannot hold


@pytest.mark.parametrize(
    "section,named",
    [
        ("  whiteboards:\n    - template: kanban\n", "kanban"),
        ("  guidance:\n    checks: [sentiment_alarm]\n", "sentiment_alarm"),
        ("  guidance:\n    checks: [clarify]\n    assertiveness: loud\n", "loud"),
        ("  quickActions:\n    - label: ''\n      prompt: x\n", "label is required"),
        ("  colour: blue\n", "colour"),
    ],
)
def test_a_playbook_naming_what_does_not_exist_is_refused_with_what_is_accepted(section, named):
    text = "---\nname: odd\ndescription: An odd playbook.\nplaybook:\n" + section + "---\n\nBody\n"
    with pytest.raises(sf.SkillError) as refused:
        sf.parse_skill_file("SKILL.md", text.encode())
    message = str(refused.value)
    assert "The playbook section is not accepted" in message and named in message
    if "template" in section:
        assert "user_stories" in message  # the templates it accepts


def test_the_editor_saves_a_playbook_through_the_same_check():
    with pytest.raises(sf.SkillError, match="whiteboard template"):
        sf.check_metadata({"playbook": {"whiteboards": [{"template": "nope"}]}})
    kept = sf.check_metadata(json.dumps({"playbook": {"guidance": {"checks": ["lookup"]}}}))
    assert kept["playbook"]["guidance"] == {"checks": ["lookup"], "assertiveness": "balanced"}


def test_a_skill_without_a_playbook_section_is_unchanged():
    assert "playbook" not in sf.check_metadata({"license": "MIT-0"})


@pytest.mark.parametrize("name", ["default", "none"])
def test_a_playbook_may_not_take_a_name_the_vp_form_uses_for_its_own_choices(name):
    text = PLAYBOOK_MD.replace("name: launch-review", f"name: {name}")
    with pytest.raises(sf.SkillError, match="cannot be named"):
        sf.parse_skill_file("SKILL.md", text.encode())
