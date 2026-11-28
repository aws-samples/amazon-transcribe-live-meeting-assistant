# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""The meeting assistant's default skills, as a deployed stack holds them.

Read-only. The configuration seeder stores the five skills LMA ships when the
stack is created or updated (see docs/meeting-assistant-skills.md#default-skills).
After a deployment each is listed by the skills API as shipped with LMA, and its
instructions can be read back. Whether they are offered for Participant use is
covered by the opt-in test_assistant_skills.py, because checking it changes a
deployment-wide setting.
"""

from __future__ import annotations

import pytest

DEFAULT_SKILLS = (
    "catch-me-up",
    "customer-call-notes",
    "decisions-and-actions",
    "follow-up-email",
    "next-meeting-prep",
)


@pytest.fixture(scope="module")
def listed(client):
    return {skill["Name"]: skill for skill in client.skills.list()}


def test_every_default_skill_is_listed_as_shipped_with_lma(listed):
    missing = [name for name in DEFAULT_SKILLS if name not in listed]
    assert missing == [], f"default skills not listed: {missing}"
    assert {listed[name]["SourceFormat"] for name in DEFAULT_SKILLS} == {"shipped"}


def test_a_default_skills_instructions_can_be_read(client, listed):
    skill = client.skills.get(listed["decisions-and-actions"]["SkillId"])
    assert skill["Description"]
    assert "current_meeting_transcript" in skill["Instructions"]
