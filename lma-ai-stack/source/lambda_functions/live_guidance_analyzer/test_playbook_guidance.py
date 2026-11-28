# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Live guidance on a meeting that runs a playbook (docs/meeting-playbooks.md).

The meeting is given only the checks its playbook names, the model is told so,
the playbook's assertiveness sets the threshold, and its item rate is never more
than the deployment's. A meeting without a playbook is analysed as before.
Uses the recorded meeting and fakes of test_index.py.
"""

from __future__ import annotations

import copy

from test_index import RECORDED, answer, ask, automatic, index, world  # noqa: F401 (fixture)


def playbook_meeting(checks, **guidance):
    meeting = copy.deepcopy(RECORDED["meeting"])
    meeting["Playbook"] = {
        "SkillId": "sprint-planning",
        "Guidance": {"Checks": list(checks), **guidance},
    }
    return meeting


def clarify():
    return ask(type="clarify", itemId=None, subject="soon", title="Clarify soon")


def prepared_text(state) -> str:
    (call,) = state["bedrock"].calls
    return call["messages"][0]["content"][0]["text"]


def test_a_playbook_meeting_is_given_only_the_checks_its_playbook_names(world):  # noqa: F811
    state = world(
        meeting=playbook_meeting(["clarify"]), answers=[answer(guidance=[ask(), clarify()])]
    )
    assert automatic()["published"] == 1
    assert [p["type"] for p in state["published"]] == ["clarify"]
    prepared = prepared_text(state)
    assert "<playbook>" in prepared and "clarify" in prepared and "ask_question" not in prepared


def test_a_meeting_without_a_playbook_is_given_every_type_as_before(world):  # noqa: F811
    state = world(answers=[answer(guidance=[ask(), clarify()])])
    assert automatic()["published"] == 2
    assert "<playbook>" not in prepared_text(state)


def test_a_quiet_playbook_raises_the_threshold_and_never_the_item_rate(world):  # noqa: F811
    world(settings={"maxItemsPer10Min": 3})
    meeting = playbook_meeting(["ask_question"], Assertiveness="quiet", MaxItemsPer10Min=40)
    settings = index.meeting_settings(meeting)
    assert settings.confidence_threshold == 0.85
    assert settings.max_items_per_10_min == 3
    assert settings.playbook_types == ("ask_question", "meeting_digest")
    assert settings.allows("ask_question") and not settings.allows("risk_probe")


def test_a_quiet_playbook_drops_what_the_deployment_threshold_would_publish(world):  # noqa: F811
    state = world(
        meeting=playbook_meeting(["ask_question"], Assertiveness="quiet"),
        answers=[answer(guidance=[ask(confidence=0.8)])],
    )
    automatic()
    assert state["published"] == []


def test_a_removed_playbook_record_without_a_skill_changes_nothing(world):  # noqa: F811
    meeting = copy.deepcopy(RECORDED["meeting"])
    meeting["Playbook"] = {"Requested": "gone", "Notes": ["no longer exists"]}
    state = world(meeting=meeting, answers=[answer(guidance=[ask(), clarify()])])
    assert automatic()["published"] == 2
    assert "<playbook>" not in prepared_text(state)
