#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""The meeting's playbook in the meeting assistant's prompt (docs/meeting-playbooks.md).

A meeting that runs a playbook has the playbook skill's instructions in the
prompt from the start, as load_skill would return them, before the fixed part
and with its own rule after the skill rule, so the fixed rules still hold. A
request that may not use the skill (turned off, or not allowed for Participant
use) is not given it. The handler is driven with the stand-ins of
test_in_meeting_request_scope.py.
"""

from __future__ import annotations

import pytest

import meeting_skills
import strands_meeting_assist_function as handler_module
from test_in_meeting_request_scope import CHAT, built, configured  # noqa: F401 -- fixtures

OWNER = {"userEmail": "owner@example.com"}

SPRINT = meeting_skills.SkillSummary(
    "sprint-planning",
    "sprint-planning",
    "Playbook for a sprint planning meeting.",
    "Sprint planning",
    "2026-11-28T00:00:00Z",
    "shipped",
)
OTHER = meeting_skills.SkillSummary(
    "launch-review", "launch-review", "Review a launch plan.", "", "2026-11-28T00:00:00Z"
)
RECORDS = {
    "sprint-planning": {"Instructions": "PLAYBOOK-STEPS: add up the estimates.", "Files": []},
    "launch-review": {"Instructions": "LAUNCH-STEPS", "Files": []},
}


@pytest.fixture
def playbook_meeting(monkeypatch):
    def install(skill_id="sprint-planning"):
        monkeypatch.setattr(meeting_skills, "load_catalog", lambda: [OTHER, SPRINT])
        monkeypatch.setattr(meeting_skills, "load_body", lambda summary: RECORDS[summary.skill_id])
        monkeypatch.setattr(meeting_skills, "meeting_playbook_id", lambda *_a, **_k: skill_id)

    return install


def test_the_meetings_playbook_is_in_the_prompt_before_the_fixed_part(built, playbook_meeting):
    playbook_meeting()
    prompt = built(OWNER)["system_prompt"]
    listed = prompt.index("<available_skills>")
    playbook = prompt.index("<meeting_playbook>")
    fixed = prompt.index(handler_module.FIXED_INSTRUCTIONS)
    rule = prompt.index(meeting_skills.PLAYBOOK_RULE)
    assert listed < playbook < fixed < rule
    assert "PLAYBOOK-STEPS" in prompt and "LAUNCH-STEPS" not in prompt
    assert "Sprint planning (sprint-planning)" in prompt
    # Its own rule follows the skill rule, inside the fixed part.
    assert prompt.index(meeting_skills.SKILL_RULES) < rule


def test_a_meeting_without_a_playbook_has_neither_the_block_nor_its_rule(built, playbook_meeting):
    playbook_meeting(skill_id="")
    prompt = built(OWNER)["system_prompt"]
    assert "<meeting_playbook>" not in prompt and meeting_skills.PLAYBOOK_RULE not in prompt


def test_a_playbook_turned_off_in_the_profile_is_not_given(built, configured, playbook_meeting):
    playbook_meeting()
    configured({"toolSelection": {"mcpServers": {}, "skills": {"sprint-planning": {"enabled": False}}}})
    prompt = built(OWNER)["system_prompt"]
    assert "PLAYBOOK-STEPS" not in prompt


def test_a_shipped_playbook_is_not_given_to_participant_use_until_allowed(
    built, configured, playbook_meeting
):
    playbook_meeting()
    configured({"inMeetingRequestTools": "selected", "inMeetingSkills": True})
    assert "PLAYBOOK-STEPS" not in built(CHAT)["system_prompt"]
    configured(
        {
            "inMeetingRequestTools": "selected",
            "inMeetingSkills": True,
            "toolSelection": {
                "mcpServers": {},
                "skills": {"sprint-planning": {"inMeetingRequests": True}},
            },
        }
    )
    assert "PLAYBOOK-STEPS" in built(CHAT)["system_prompt"]


def test_a_playbook_whose_skill_is_gone_is_left_out(built, playbook_meeting):
    playbook_meeting(skill_id="deleted-playbook")
    assert "<meeting_playbook>" not in built(OWNER)["system_prompt"]


def test_the_playbook_rule_keeps_the_fixed_rules_ahead_of_it():
    assert "The rule above applies to them" in meeting_skills.PLAYBOOK_RULE
    assert meeting_skills.playbook_prompt(SPRINT, None) == ""


def test_the_meetings_playbook_is_read_from_its_record():
    class Table:
        def get_item(self, **kwargs):
            assert kwargs["Key"] == {"PK": "c#call-1", "SK": "c#call-1"}
            return {"Item": {"Playbook": {"SkillId": "sprint-planning"}}}

    class Broken:
        def get_item(self, **_kwargs):
            raise RuntimeError("unreadable")

    assert meeting_skills.meeting_playbook_id(Table(), "call-1") == "sprint-planning"
    assert meeting_skills.meeting_playbook_id(Broken(), "call-1") == ""
    assert meeting_skills.meeting_playbook_id(Table(), "") == ""
