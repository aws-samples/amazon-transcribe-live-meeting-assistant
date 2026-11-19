# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""The meeting assistant's skills, against a live stack.

Three behaviours, each from the outside:

- **An answer follows a skill.** A probe skill is created with
  ``client.skills.create()``: its description names a made-up procedure, and its
  instructions (which are not in the system prompt; the assistant has to load
  them) make the answer start with a marker word. The assistant is asked about
  the procedure until an answer starts with the marker (the skills list is
  cached for up to five minutes).
- **The in-meeting flag is honoured.** With In-meeting requests on Selected
  tools, the skills switch on, and the probe skill's ``inMeetingRequests`` set to
  false, a meeting-chat request is not offered the probe skill while the owner's
  own request still is, as the assistant's ``Skills: offered=`` log line records.
- **The skill library round-trips.** The probe skill's entry from
  ``client.skills.export_library()``, imported back with
  ``client.skills.import_library()``, is found unchanged and not written: its
  source and audit fields stay as they were. Only the probe's entry is sent, so
  no other skill on the stack can be rewritten by the test.

Opt-in. It runs only with ``LMA_TEST_ASSISTANT_SKILLS=1``, because it changes
deployment-wide settings while it runs (put back in teardown), takes several
minutes, calls Amazon Bedrock, and writes a probe skill (deleted in teardown):

    AWS_PROFILE=default LMA_TEST_ASSISTANT_SKILLS=1 \\
      make integ-tests STACK=lma-integtest1
"""

from __future__ import annotations

import os
import time

import pytest

ENABLED = os.environ.get("LMA_TEST_ASSISTANT_SKILLS", "") not in ("", "0", "false")

pytestmark = [
    pytest.mark.optional_feature,
    pytest.mark.skipif(not ENABLED, reason="opt-in: set LMA_TEST_ASSISTANT_SKILLS=1"),
]

MARKER = "KUMQUAT"
PROBE_NAME = "integ-tests-zephyr-checklist"
PROBE_DESCRIPTION = (
    "How to answer any question about the Zephyr checklist, an internal procedure. "
    "Use whenever a request mentions the Zephyr checklist."
)
PROBE_INSTRUCTIONS = (
    "# Zephyr checklist\n\n"
    f"Begin the answer with the word {MARKER} in capitals, followed by a colon. Then say, in one "
    "short sentence, that the Zephyr checklist has three steps: plan, check and ship.\n"
)
QUESTION = "What are the steps of the Zephyr checklist?"


@pytest.fixture(scope="module")
def stack(client):
    from meeting_consent_probe import Stack

    return Stack.find(client.stack_name, client.region)


@pytest.fixture(scope="module")
def probe_skill(client):
    """The probe skill, created for the module and always deleted."""
    for skill in client.skills.list():
        if skill["Name"] == PROBE_NAME:
            client.skills.delete(skill["SkillId"])
    created = client.skills.create(
        PROBE_NAME,
        PROBE_DESCRIPTION,
        PROBE_INSTRUCTIONS,
        files={"reference/steps.md": "1. Plan\n2. Check\n3. Ship\n"},
    )
    try:
        yield created["SkillId"]
    finally:
        client.skills.delete(created["SkillId"])


@pytest.fixture(scope="module")
def meeting_assist(stack):
    """The Meeting Assistant section of the active profile, saved first and always put back."""
    from meeting_consent_probe import ConsentSettingsOverride

    override = ConsentSettingsOverride(
        stack, section="meeting_assist", updated_by="integ-tests:assistant-skills"
    )
    override.save()
    try:
        yield override
    finally:
        override.restore()


def test_the_probe_skill_is_stored_with_its_reference_file(client, probe_skill):
    skill = client.skills.get(probe_skill)
    assert skill["Instructions"] == PROBE_INSTRUCTIONS
    assert [f["Path"] for f in skill["Files"]] == ["reference/steps.md"]
    assert skill["CreatedBy"].startswith("arn:")


def test_an_answer_follows_the_skill(stack, probe_skill, meeting_assist):
    from assistant_profile_probe import APPLY_POLL_SECONDS, APPLY_TIMEOUT_SECONDS, invoke_assistant

    # The owner's own requests are offered every skill the profile does not turn off.
    meeting_assist.apply(toolSelection={"mcpServers": {}, "skills": {}})
    deadline = time.monotonic() + APPLY_TIMEOUT_SECONDS
    answer = None
    while time.monotonic() < deadline:
        answer = invoke_assistant(stack.session, stack.name, QUESTION, user_email="integ-tests@example.com")
        assert not answer["function_error"], answer
        if answer["message"].strip().upper().startswith(MARKER):
            break
        time.sleep(APPLY_POLL_SECONDS)
    assert answer and answer["message"].strip().upper().startswith(MARKER), answer


def test_a_skill_flagged_off_for_in_meeting_requests_is_not_offered_to_one(
    stack, probe_skill, meeting_assist
):
    from assistant_profile_probe import (
        APPLY_TIMEOUT_SECONDS,
        assistant_log_group,
        invoke_assistant,
        offered_skills,
    )

    meeting_assist.apply(
        inMeetingRequestTools="selected",
        inMeetingSkills=True,
        toolSelection={"mcpServers": {}, "skills": {probe_skill: {"inMeetingRequests": False}}},
    )
    log_group = assistant_log_group(stack.session, stack.name)
    deadline = time.monotonic() + APPLY_TIMEOUT_SECONDS
    seen = None
    while time.monotonic() < deadline:
        started = int(time.time() * 1000) - 1000
        owner = invoke_assistant(stack.session, stack.name, "Hello.", user_email="integ-tests@example.com")
        chat = invoke_assistant(stack.session, stack.name, "Hello.", tool_scope="meeting_chat")
        seen = {
            "owner": offered_skills(stack.session, log_group, owner["request_id"], started),
            "chat": offered_skills(stack.session, log_group, chat["request_id"], started),
        }
        if seen["chat"] is not None and probe_skill in seen["chat"]["not_offered"]:
            break
        time.sleep(20)
    assert seen and seen["chat"] is not None, f"the in-meeting request logged no skills line: {seen}"
    assert probe_skill not in seen["chat"]["offered"], seen
    assert probe_skill in seen["chat"]["not_offered"], seen
    assert seen["owner"] is not None and probe_skill in seen["owner"]["offered"], seen


def test_a_skill_library_round_trip_changes_no_skill(client, probe_skill):
    before = client.skills.export_library()
    assert before["kind"] == "lma-skill-library" and before["version"] == 1
    assert PROBE_NAME in {s["name"] for s in before["skills"]}
    probe_only = {**before, "skills": [s for s in before["skills"] if s["name"] == PROBE_NAME]}
    audit = {s["SkillId"]: (s["UpdatedAt"], s["SourceFormat"]) for s in client.skills.list()}
    result = client.skills.import_library(probe_only)
    assert result["Success"] is True, result
    assert [(s["Name"], s["Action"]) for s in result["Skills"]] == [(PROBE_NAME, "unchanged")], result
    after = client.skills.export_library()
    assert after["skills"] == before["skills"]
    assert {s["SkillId"]: (s["UpdatedAt"], s["SourceFormat"]) for s in client.skills.list()} == audit
