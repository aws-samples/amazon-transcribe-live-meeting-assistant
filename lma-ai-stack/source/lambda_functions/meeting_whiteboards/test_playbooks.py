#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Applying a meeting playbook, and the options offered to choose one (no AWS).

What a caller depends on: applying a playbook makes its whiteboards with their
templates and turns on only what it names; switching to another and removing
it put back what was on before; who may apply one; a playbook whose skill was
deleted, or that names a template or check this release does not know, applies
what it can and says so; the meeting's whiteboard cap is kept; and the options
suggest a playbook from a meeting's title. The meetings table is the whiteboard
tests' fake; AppSync calls are routed back into the same function.
"""

from __future__ import annotations

import copy
import sys
from typing import Any, Dict, List

import pytest
import test_index as base
from test_index import BACKEND, CALL, OTHER, OWNER, SHARED, table, user  # noqa: F401 (fixture)

index = base.index
# playbooks.py imports the function's handler module by the name Lambda gives it.
sys.modules["index"] = index
import playbooks  # noqa: E402  pylint: disable=wrong-import-position
from lma_whiteboards import rules  # noqa: E402  pylint: disable=wrong-import-position

ACCOUNT = "123456789012"
ADMIN = user("admin@example.com", admin=True)
PARTICIPANT = {
    "username": "p@example.com",
    "claims": {"sub": "p", "cognito:groups": ["Participant"]},
}

SPRINT = {
    "suggestFor": ["sprint planning"],
    "whiteboards": [
        {"template": "user_stories", "title": "Sprint backlog", "instructions": "Stories."},
        {"template": "gantt", "title": "Sprint plan"},
        {"template": "risks", "title": "Risks", "assistantUpdates": False},
    ],
    "guidance": {"checks": ["action_missing_owner", "risk_probe"], "maxItemsPer10Min": 6},
    "quickActions": [{"label": "Scope vs capacity", "prompt": "Add up the estimates."}],
}
RETRO = {
    "suggestFor": ["retrospective"],
    "whiteboards": [{"template": "retrospective", "title": "Retrospective"}],
    "guidance": {"checks": ["action_missing_owner"], "assertiveness": "quiet"},
    "quickActions": [{"label": "Themes", "prompt": "Group the themes."}],
}


class Table(base.FakeTable):
    """The meetings table, with the skills table beside it and the playbook's write."""

    def __init__(self) -> None:
        super().__init__()
        self.skills: Dict[str, Dict[str, Any]] = {}

    def query(self, **kwargs: Any) -> Dict[str, Any]:
        if kwargs.get("TableName") == "skills":
            return {"Items": [self._serialized(s) for s in self.skills.values()]}
        return super().query(**kwargs)

    def get_item(self, **kwargs: Any) -> Dict[str, Any]:
        if kwargs.get("TableName") == "skills":
            skill = self.skills.get(kwargs["Key"]["SkillId"]["S"])
            return {"Item": self._serialized(skill)} if skill else {}
        return super().get_item(**kwargs)

    def _apply_update(self, key, expression, names, values):
        if "#pu = :pu" not in expression:
            return super()._apply_update(key, expression, names, values)
        row = self.rows[key]
        sets, _, removes = expression[len("SET ") :].partition(" REMOVE ")
        for clause in sets.split(", "):
            name, value = (part.strip() for part in clause.split("="))
            row[names[name]] = index._plain(base.DES.deserialize(values[value]))
        for name in filter(None, (r.strip() for r in removes.split(","))):
            row.pop(names[name], None)
        return row


def skill(skill_id: str, playbook: Any, **extra: Any) -> Dict[str, Any]:
    return {
        "AccountId": ACCOUNT,
        "SkillId": skill_id,
        "Name": skill_id,
        "DisplayName": extra.pop("DisplayName", skill_id.replace("-", " ").capitalize()),
        "Description": "A playbook.",
        "Metadata": {"playbook": playbook} if playbook is not None else {},
        "SourceFormat": "shipped",
        **extra,
    }


@pytest.fixture
def world(table, monkeypatch):  # noqa: F811 (the fixture)
    fake = Table()
    fake.rows = table.rows
    fake.skills = {
        "sprint-planning": skill("sprint-planning", SPRINT, DisplayName="Sprint planning"),
        "retrospective": skill("retrospective", RETRO, DisplayName="Retrospective"),
        "plain-skill": skill("plain-skill", None),
    }
    monkeypatch.setattr(index, "DYNAMODB", fake)
    monkeypatch.setattr(playbooks, "SKILLS_TABLE", "skills")
    monkeypatch.setattr(playbooks, "SKILLS_ACCOUNT_ID", ACCOUNT)
    state = {
        "guidance": {"intervalSeconds": 90, "maxRunsPerMeeting": 120, "maxItemsPer10Min": 10},
        "whiteboards": {},
        "default": "",
        "graphql": [],
    }
    monkeypatch.setattr(playbooks, "guidance_settings", lambda: dict(state["guidance"]))
    monkeypatch.setattr(playbooks, "whiteboard_settings_raw", lambda: dict(state["whiteboards"]))
    monkeypatch.setattr(playbooks, "default_playbook", lambda: state["default"])
    monkeypatch.setattr(
        playbooks,
        "_price_lookup",
        lambda: (
            lambda _model: {
                "inputTokens": 1e-07,
                "outputTokens": 5e-07,
                "cacheReadInputTokens": 1e-08,
                "cacheWriteInputTokens": 1.25e-07,
            }
        ),
    )
    monkeypatch.setattr(playbooks, "_model", lambda _setting: "global.anthropic.claude-haiku-5-5")
    monkeypatch.setattr(
        index, "load_settings", lambda: rules.Settings.from_raw(state["whiteboards"])
    )

    def graphql(document: str, variables: Dict[str, Any]) -> Dict[str, Any]:
        state["graphql"].append(document.split("(")[0])
        data = variables["input"]
        if "createMeetingWhiteboard" in document:
            return {
                "createMeetingWhiteboard": index.create_whiteboard(
                    index.caller_of({"identity": BACKEND}), data
                )
            }
        if "deleteMeetingWhiteboard" in document:
            return {
                "deleteMeetingWhiteboard": index.delete_whiteboard(
                    index.caller_of({"identity": BACKEND}), data
                )
            }
        if "applyMeetingPlaybook" in document:
            return {
                "applyMeetingPlaybook": playbooks.apply(
                    index.caller_of({"identity": BACKEND}), data
                )
            }
        raise AssertionError(document)

    monkeypatch.setattr(playbooks, "appsync_graphql", graphql)
    state["table"] = fake
    return state


def apply(identity=None, **data) -> Dict[str, Any]:
    return base.call(
        "applyMeetingPlaybook", identity or user(OWNER), input={"CallId": CALL, **data}
    )


def options(identity=None, **arguments) -> Dict[str, Any]:
    return base.call("meetingPlaybookOptions", identity or user(OWNER), **arguments)


def boards() -> List[Dict[str, Any]]:
    """The meeting's whiteboards by title (made within one millisecond, their order is the id's)."""
    return sorted(index.read_boards(CALL), key=lambda b: b["Title"])


def meeting(world) -> Dict[str, Any]:
    return world["table"].rows[(f"c#{CALL}", f"c#{CALL}")]


# ── applying ─────────────────────────────────────────────────────────────────


def test_applying_makes_its_whiteboards_and_turns_on_only_what_it_names(world) -> None:
    result = apply(SkillId="sprint-planning")
    made = boards()
    assert [(b["Title"], b["Template"], b["AssistantUpdates"]) for b in made] == [
        ("Risks", "risks", False),
        ("Sprint backlog", "user_stories", True),
        ("Sprint plan", "gantt", True),
    ]
    # The assistant's panels, so its updates fill them; and the board's instructions.
    assert all(p["Source"] == "assistant" for b in made for p in b["Panels"])
    assert made[1]["Instructions"] == "Stories."
    assert made[1]["CreatedBy"] == "Playbook: Sprint planning"
    assert result["GuidanceEnabled"] is True and result["WhiteboardsEnabled"] is True
    record = result["Playbook"]
    assert record["SkillId"] == "sprint-planning" and record["Origin"] == "chosen"
    assert record["AppliedBy"] == OWNER
    assert record["Guidance"] == {
        "Checks": ["action_missing_owner", "risk_probe"],
        "Assertiveness": "balanced",
        "MaxItemsPer10Min": 6,
    }
    assert record["QuickActions"] == [
        {"Label": "Scope vs capacity", "Prompt": "Add up the estimates."}
    ]
    assert record["Previous"] == {"GuidanceEnabled": False, "WhiteboardsEnabled": False}
    assert result["PlaybookUpdatedAt"]


def test_a_playbook_with_no_kept_whiteboards_leaves_the_whiteboard_updates_off(world) -> None:
    world["table"].skills["quiet"] = skill(
        "quiet",
        {"whiteboards": [{"template": "blank", "assistantUpdates": False}]},
    )
    result = apply(SkillId="quiet")
    assert result["WhiteboardsEnabled"] is False and result["GuidanceEnabled"] is False
    assert [b["Title"] for b in boards()] == ["Blank"]


def test_switching_keeps_what_was_on_before_the_first_and_reuses_nothing_twice(world) -> None:
    meeting(world)["GuidanceEnabled"] = True  # on before any playbook
    apply(SkillId="sprint-planning")
    switched = apply(SkillId="retrospective")
    assert switched["Playbook"]["SkillId"] == "retrospective"
    assert switched["Playbook"]["Previous"] == {
        "GuidanceEnabled": True,
        "WhiteboardsEnabled": False,
    }
    assert [b["Title"] for b in boards()] == [
        "Retrospective",
        "Risks",
        "Sprint backlog",
        "Sprint plan",
    ]
    # Back again: the boards of the same title and template are the ones it made.
    back = apply(SkillId="sprint-planning")
    assert len(boards()) == 4
    assert [b.get("Reused") for b in back["Playbook"]["Whiteboards"]] == [True, True, True]


def test_removing_puts_back_what_was_on_and_keeps_the_whiteboards(world) -> None:
    apply(SkillId="sprint-planning")
    removed = apply(SkillId=None)
    assert "Playbook" not in removed or removed["Playbook"] is None
    assert removed["GuidanceEnabled"] is False and removed["WhiteboardsEnabled"] is False
    assert removed["PlaybookUpdatedAt"]
    assert len(boards()) == 3


def test_removing_with_delete_whiteboards_deletes_only_those_nobody_wrote_on(world) -> None:
    apply(SkillId="sprint-planning")
    first = next(b for b in boards() if b["Title"] == "Sprint backlog")
    panels = [{k: p[k] for k in ("PanelId", "Title", "Kind", "Content")} for p in first["Panels"]]
    panels[0]["Content"] = "- As a technician I want offline notes"
    base.update(first, panels)
    removed = apply(SkillId="", DeleteWhiteboards=True)
    assert [b["Title"] for b in boards()] == ["Sprint backlog"]
    assert removed["GuidanceEnabled"] is False


def test_the_meetings_whiteboard_cap_is_kept_and_noted(world) -> None:
    world["whiteboards"] = {"maxWhiteboardsPerMeeting": 2}
    result = apply(SkillId="sprint-planning")
    assert len(boards()) == 2
    assert any("the most this deployment allows" in note for note in result["Playbook"]["Notes"])


def test_a_template_or_check_this_release_does_not_know_is_left_out_and_noted(world) -> None:
    world["table"].skills["odd"] = skill(
        "odd",
        {
            "whiteboards": [
                {"template": "kanban", "title": "Board"},
                {"template": "decisions", "title": "Decisions"},
            ],
            "guidance": {"checks": ["sentiment_alarm", "clarify"]},
        },
    )
    result = apply(SkillId="odd")
    assert [b["Title"] for b in boards()] == ["Decisions"]
    assert result["Playbook"]["Guidance"]["Checks"] == ["clarify"]
    notes = " ".join(result["Playbook"]["Notes"])
    assert "kanban" in notes and "sentiment_alarm" in notes


def test_research_checks_off_in_the_deployment_are_noted_not_run(world) -> None:
    world["table"].skills["arch"] = skill(
        "arch", {"guidance": {"checks": ["fact_check", "risk_probe"]}}
    )
    result = apply(SkillId="arch")
    assert result["GuidanceEnabled"] is True
    assert any("Fact checks are turned off" in n for n in result["Playbook"]["Notes"])


def test_guidance_off_for_the_deployment_is_not_turned_on(world) -> None:
    world["guidance"]["maxRunsPerMeeting"] = 0
    result = apply(SkillId="retrospective")
    assert result["GuidanceEnabled"] is False
    assert any("Live guidance is turned off" in n for n in result["Playbook"]["Notes"])


@pytest.mark.parametrize("identity", [user(OWNER), ADMIN])
def test_the_owner_and_an_admin_apply_a_playbook(world, identity) -> None:
    assert apply(identity, SkillId="retrospective")["Playbook"]["SkillId"] == "retrospective"


@pytest.mark.parametrize("identity", [user(SHARED), user(OTHER), PARTICIPANT])
def test_anyone_else_is_refused_and_nothing_changes(world, identity) -> None:
    with base.refused("Unauthorized"):
        apply(identity, SkillId="retrospective")
    assert boards() == [] and "Playbook" not in meeting(world)


def test_a_participant_is_not_offered_the_options(world) -> None:
    with base.refused("Unauthorized"):
        options(PARTICIPANT)


def test_a_skill_that_is_not_a_playbook_or_does_not_exist_is_refused(world) -> None:
    for skill_id in ("plain-skill", "nope"):
        with base.refused("BadRequest"):
            apply(SkillId=skill_id)
    assert boards() == []


def test_an_ended_meeting_takes_no_new_playbook_but_can_drop_one(world) -> None:
    apply(SkillId="retrospective")
    meeting(world)["Status"] = "ENDED"
    with base.refused("BadRequest"):
        apply(SkillId="sprint-planning")
    assert apply(SkillId=None)["GuidanceEnabled"] is False


# ── at a meeting's start ─────────────────────────────────────────────────────


def start(world, skill_id, origin="virtual-participant"):
    return index.handler(
        {
            "PlaybookApply": {
                "CallId": CALL,
                "SkillId": skill_id,
                "Origin": origin,
                "UpdatedBy": OWNER,
            }
        },
        None,
    )


def test_a_virtual_participants_playbook_is_applied_through_appsync_at_the_start(world) -> None:
    assert start(world, "sprint-planning") == {"applied": True}
    record = meeting(world)["Playbook"]
    assert record["Origin"] == "virtual-participant" and record["AppliedBy"] == OWNER
    assert len(boards()) == 3
    assert "mutation ApplyMeetingPlaybook" in world["graphql"]


def test_a_playbook_deleted_since_the_vp_was_scheduled_is_noted_and_none_applied(world) -> None:
    del world["table"].skills["sprint-planning"]
    start(world, "sprint-planning")
    record = meeting(world)["Playbook"]
    assert "SkillId" not in record and record["Requested"] == "sprint-planning"
    assert "no longer exists" in record["Notes"][0]
    assert boards() == [] and meeting(world).get("GuidanceEnabled") is not True


def test_a_playbook_chosen_on_the_page_first_is_not_replaced_by_the_default(world) -> None:
    apply(SkillId="retrospective")
    assert start(world, "sprint-planning", origin="profile-default")["applied"] is False
    assert meeting(world)["Playbook"]["SkillId"] == "retrospective"


def test_none_at_the_start_applies_nothing(world) -> None:
    assert start(world, "none")["applied"] is False
    assert "Playbook" not in meeting(world)


# ── the options ──────────────────────────────────────────────────────────────


def test_the_options_list_each_playbook_with_what_it_turns_on_and_its_cost(world) -> None:
    world["default"] = "retrospective"
    result = options(Title="Sprint 42 planning")
    by_id = {p["SkillId"]: p for p in result["Playbooks"]}
    assert set(by_id) == {"sprint-planning", "retrospective"}  # not the plain skill
    sprint = by_id["sprint-planning"]
    assert [w["templateLabel"] for w in sprint["Whiteboards"]] == [
        "User stories",
        "Project plan (Gantt chart)",
        "Risk register",
    ]
    assert [c["check"] for c in sprint["Guidance"]["checks"]] == [
        "action_missing_owner",
        "risk_probe",
    ]
    assert sprint["Estimate"]["perHourAtMost"] > 0
    assert {line["what"] for line in sprint["Estimate"]["lines"]} == {
        "Live guidance",
        "Meeting whiteboards",
    }
    assert result["Suggestions"][0]["SkillId"] == "sprint-planning"
    assert result["ProfileDefault"] == "retrospective"
    assert result["Current"] is None


def test_the_options_for_a_meeting_suggest_from_its_name_and_show_its_playbook(world) -> None:
    meeting(world)["CallId"] = "Sprint 41 retrospective - 2026-11-28-10:00:00.000"
    apply(SkillId="retrospective")
    result = options(CallId=CALL)
    assert result["Suggestions"][0]["SkillId"] == "retrospective"
    assert result["Current"]["SkillId"] == "retrospective"
    with base.refused("Unauthorized"):
        options(user(OTHER), CallId=CALL)


def test_nothing_is_suggested_without_a_match(world) -> None:
    assert options(Title="Weekly sync")["Suggestions"] == []


def test_the_apply_mutation_selects_every_field_the_meeting_page_subscribes_to() -> None:
    subscription = (
        base.HERE.parents[1] / "ui" / "src" / "graphql" / "queries" / "onUpdateCall.js"
    ).read_text(encoding="utf-8")
    selected = {
        line.strip()
        for line in subscription.splitlines()
        if line.strip() and line.strip()[0].isupper()
    }
    assert "PlaybookUpdatedAt" in selected
    assert "Playbook {" in selected
    assert sorted(f for f in selected if f not in index.SUBSCRIBED_CALL_FIELDS) == []


def test_a_stored_record_holds_no_empty_values(world) -> None:
    apply(SkillId="retrospective")
    record = copy.deepcopy(meeting(world)["Playbook"])
    assert record["Guidance"]["Assertiveness"] == "quiet"
    assert "MaxItemsPer10Min" not in record["Guidance"]


def test_a_run_over_several_whiteboards_may_write_more_before_it_is_cut_short() -> None:
    assert index.run_max_tokens(1) == 2500
    assert index.run_max_tokens(3) == 5500  # a playbook's three whiteboards
    assert index.run_max_tokens(10) == 8000


def test_switching_to_a_playbook_that_is_gone_deletes_nothing(world) -> None:
    apply(SkillId="sprint-planning")
    with base.refused("BadRequest"):
        apply(SkillId="nope", DeleteWhiteboards=True)
    assert len(boards()) == 3
    assert meeting(world)["Playbook"]["SkillId"] == "sprint-planning"


def test_a_whiteboard_the_meeting_already_had_is_kept_when_the_playbook_goes(world) -> None:
    base.create(Title="Retrospective", Template="retrospective")
    applied = apply(SkillId="retrospective")
    assert applied["Playbook"]["Whiteboards"][0]["Reused"] is True
    apply(SkillId=None, DeleteWhiteboards=True)
    assert [b["Title"] for b in boards()] == ["Retrospective"]


def test_a_guidance_part_whose_checks_are_all_unknown_leaves_live_guidance_alone(world) -> None:
    meeting(world)["GuidanceEnabled"] = True
    world["table"].skills["odd"] = skill(
        "odd",
        {
            "guidance": {"checks": ["sentiment_alarm"]},
            "quickActions": [{"label": "a", "prompt": "b"}],
        },
    )
    applied = apply(SkillId="odd")
    assert "Guidance" not in applied["Playbook"]
    assert applied["GuidanceEnabled"] is True


def test_reapplying_the_current_playbook_keeps_its_whiteboards_its_own(world) -> None:
    apply(SkillId="retrospective")
    again = apply(SkillId="retrospective")
    assert [b.get("Reused") for b in again["Playbook"]["Whiteboards"]] == [None]
    apply(SkillId=None, DeleteWhiteboards=True)
    assert boards() == []


def test_a_playbook_whose_only_checks_are_off_here_leaves_live_guidance_alone(world) -> None:
    world["table"].skills["research"] = skill(
        "research",
        {
            "guidance": {"checks": ["fact_check", "lookup"]},
            "quickActions": [{"label": "a", "prompt": "b"}],
        },
    )
    applied = apply(SkillId="research")
    assert applied["GuidanceEnabled"] is False
    assert any("None of its checks can run" in n for n in applied["Playbook"]["Notes"])
