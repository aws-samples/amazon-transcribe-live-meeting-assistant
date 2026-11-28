# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""The meeting playbook rules (no AWS): reading one, the suggestion, what a meeting runs with, the cost."""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lma_playbooks import rules  # noqa: E402

SPRINT = {
    "suggestFor": ["sprint planning", "iteration planning"],
    "whiteboards": [
        {"template": "user_stories", "title": "Sprint backlog"},
        {"template": "gantt", "assistantUpdates": False},
    ],
    "guidance": {"checks": ["action_missing_owner", "fact_check"], "maxItemsPer10Min": 6},
    "quickActions": [{"label": "Scope", "prompt": "Add up the estimates."}],
}


def skill(skill_id, playbook, display=""):
    return {
        "SkillId": skill_id,
        "Name": skill_id,
        "DisplayName": display,
        "Metadata": {"playbook": playbook},
    }


# ── reading ──────────────────────────────────────────────────────────────────


def test_a_playbook_is_read_with_its_defaults_filled_in():
    playbook = rules.parse_playbook(SPRINT)
    assert playbook["whiteboards"][1] == {
        "template": "gantt",
        "title": "Project plan (Gantt chart)",  # the template's label
        "instructions": "",
        "assistantUpdates": False,
    }
    assert playbook["guidance"]["assertiveness"] == "balanced"
    assert rules.parse_playbook(playbook) == playbook  # stored form is stable


def test_every_problem_is_listed_at_once():
    with pytest.raises(rules.PlaybookError) as refused:
        rules.parse_playbook(
            {
                "whiteboards": [{"template": "kanban"}, {"template": "risks", "title": "x" * 121}],
                "guidance": {"checks": ["nope"], "maxItemsPer10Min": 0},
                "quickActions": [{"label": "", "prompt": "p"}],
                "extra": 1,
            }
        )
    joined = " | ".join(refused.value.problems)
    for expected in (
        "kanban",
        "longer than 120",
        "'nope'",
        "maxItemsPer10Min",
        "label is required",
        "extra",
    ):
        assert expected in joined


def test_leniently_the_rest_is_applied_and_each_part_left_out_is_noted():
    playbook, notes = rules.lenient_playbook(
        {
            "whiteboards": [{"template": "kanban"}, {"template": "risks"}],
            "guidance": {"checks": ["clarify", "x"]},
        }
    )
    assert [b["template"] for b in playbook["whiteboards"]] == ["risks"]
    assert playbook["guidance"]["checks"] == ["clarify"]
    assert len(notes) == 2


def test_the_limits_hold():
    many = {
        "whiteboards": [{"template": "blank"}] * 6,
        "quickActions": [{"label": "a", "prompt": "b"}] * 7,
    }
    _playbook, notes = rules.lenient_playbook(many)
    assert any("at most 5 whiteboards" in n for n in notes)
    assert any("at most 6 quick actions" in n for n in notes)


def test_a_skill_is_a_playbook_only_with_a_section():
    assert rules.is_playbook(skill("a", SPRINT))
    assert not rules.is_playbook({"Metadata": {"license": "MIT-0"}})
    assert not rules.is_playbook(None)


def test_the_check_names_are_the_live_guidance_types():
    assert set(rules.GUIDANCE_CHECKS) == {
        "ask_question",
        "uncovered_topic",
        "clarify",
        "risk_probe",
        "action_missing_owner",
        "action_missing_due_date",
        "time_check",
        "fact_check",
        "lookup",
    }


def test_decimals_read_back_from_dynamodb_are_plain_numbers():
    assert rules.plain({"a": [Decimal(6), Decimal("0.5")], "b": True}) == {"a": [6, 0.5], "b": True}


# ── the suggestion ───────────────────────────────────────────────────────────

SHIPPED = [
    skill("sprint-planning", {"suggestFor": ["sprint planning"]}, "Sprint planning"),
    skill("retrospective", {"suggestFor": ["retrospective", "retro"]}, "Retrospective"),
    skill(
        "incident-review",
        {"suggestFor": ["incident review", "post incident", "postmortem"]},
        "Incident review",
    ),
    skill(
        "customer-discovery",
        {"suggestFor": ["customer discovery", "discovery call"]},
        "Customer discovery",
    ),
    skill(
        "architecture-review",
        {"suggestFor": ["architecture review", "design review"]},
        "Architecture review",
    ),
]


@pytest.mark.parametrize(
    "title,expected",
    [
        ("Sprint 42 planning - Orbit field app", "sprint-planning"),
        ("Sprint 41 retrospective - Orbit field app", "retrospective"),
        ("Incident review - checkout latency on 24 November", "incident-review"),
        ("Post-incident retro", "incident-review"),
        ("Customer discovery - Tidewater Freight Cooperative", "customer-discovery"),
        ("Architecture review - Ledgerline event ingestion", "architecture-review"),
    ],
)
def test_the_title_suggests_the_matching_playbook_first(title, expected):
    suggested = rules.suggest(SHIPPED, title)
    assert suggested[0]["SkillId"] == expected
    assert suggested[0]["MatchedIn"] == "title"


def test_nothing_is_suggested_without_a_whole_word_match():
    assert rules.suggest(SHIPPED, "Quarterly roadmap sync - Pinecrest Studio") == []
    assert rules.suggest(SHIPPED, "Retrofit planning") == []


def test_the_agenda_suggests_too_but_below_the_title():
    suggested = rules.suggest(SHIPPED, "Weekly sync", agenda="1. Quick retro on the release")
    assert suggested[0]["SkillId"] == "retrospective" and suggested[0]["MatchedIn"] == "agenda"
    both = rules.suggest(SHIPPED, "Design review", agenda="then a retro")
    assert [s["SkillId"] for s in both] == ["architecture-review", "retrospective"]


# ── what a meeting runs with ─────────────────────────────────────────────────

DEPLOYMENT = {"confidenceThreshold": 0.7, "maxItemsPer10Min": 10, "factChecksEnabled": True}


def meeting_with(**guidance):
    return {"Playbook": {"SkillId": "x", "Guidance": guidance}}


def test_without_a_playbook_the_deployments_settings_and_every_type():
    settings, types = rules.effective_guidance_settings(DEPLOYMENT, {})
    assert settings == DEPLOYMENT and types is None


def test_a_playbook_names_the_types_and_its_assertiveness_sets_the_threshold():
    settings, types = rules.effective_guidance_settings(
        DEPLOYMENT,
        meeting_with(Checks=["clarify", "fact_check", "lookup"], Assertiveness="assertive"),
    )
    # lookups are off in the deployment, so a playbook cannot turn them on.
    assert types == ["clarify", "fact_check", "meeting_digest"]
    assert settings["confidenceThreshold"] == 0.65
    assert settings["factChecksEnabled"] is True and settings["lookupsEnabled"] is False


def test_a_playbook_never_raises_the_item_rate_above_the_deployments():
    settings, _ = rules.effective_guidance_settings(
        DEPLOYMENT, meeting_with(Checks=["clarify"], MaxItemsPer10Min=40)
    )
    assert settings["maxItemsPer10Min"] == 10
    settings, _ = rules.effective_guidance_settings(
        DEPLOYMENT, meeting_with(Checks=["clarify"], MaxItemsPer10Min=4)
    )
    assert settings["maxItemsPer10Min"] == 4


def test_a_playbook_without_fact_checks_turns_them_off_for_the_meeting():
    settings, _ = rules.effective_guidance_settings(DEPLOYMENT, meeting_with(Checks=["clarify"]))
    assert settings["factChecksEnabled"] is False


def test_quick_actions_are_read_from_the_meeting():
    meeting = {"Playbook": {"QuickActions": [{"Label": "A", "Prompt": "B"}, {"Label": ""}]}}
    assert rules.quick_actions_of(meeting) == [{"Label": "A", "Prompt": "B"}]


# ── the cost ─────────────────────────────────────────────────────────────────

PRICE = {
    "inputTokens": 1e-07,
    "outputTokens": 5e-07,
    "cacheReadInputTokens": 1e-08,
    "cacheWriteInputTokens": 1.25e-07,
}


def estimate(playbook, price=PRICE, **guidance):
    return rules.estimate_cost(
        rules.parse_playbook(playbook),
        {"intervalSeconds": 90, "maxRunsPerMeeting": 120, **guidance},
        {"intervalSeconds": 180, "maxRunsPerMeeting": 60},
        lambda _model: price,
        guidance_model="m",
        whiteboard_model="m",
    )


def test_the_estimate_counts_the_runs_each_part_turns_on():
    result = estimate(SPRINT, factChecksEnabled=True)
    by_what = {line["what"]: line for line in result["lines"]}
    assert by_what["Live guidance"]["runsPerHourAtMost"] == 40
    assert by_what["Live guidance"]["runsPerMeetingAtMost"] == 120
    assert by_what["Meeting whiteboards"]["runsPerHourAtMost"] == 20
    assert "Live guidance fact checks and lookups" in by_what
    # Haiku 5.5 prices: a few cents an hour, as docs/live-guidance.md measures.
    assert 0.03 < result["perHourAtMost"] < 0.15


def test_research_is_not_counted_when_the_deployment_has_it_off():
    lines = {line["what"] for line in estimate(SPRINT)["lines"]}
    assert "Live guidance fact checks and lookups" not in lines


def test_a_playbook_that_turns_nothing_on_costs_nothing():
    result = estimate({"whiteboards": [{"template": "blank", "assistantUpdates": False}]})
    assert result["lines"] == [] and result["perHourAtMost"] == 0.0


def test_an_unpriced_model_is_said_to_be_unpriced_not_guessed():
    result = rules.estimate_cost(
        rules.parse_playbook(SPRINT),
        {},
        {},
        lambda _model: None,
        guidance_model="m",
        whiteboard_model="m",
    )
    assert result["unpriced"] == ["Live guidance", "Meeting whiteboards"]
    assert result["perHourAtMost"] == 0.0


def test_a_guidance_part_naming_no_known_check_is_no_guidance_part():
    settings, types = rules.effective_guidance_settings(
        DEPLOYMENT, meeting_with(Checks=["unknown"])
    )
    assert types is None and settings == DEPLOYMENT


def test_research_only_checks_off_in_the_deployment_are_no_guidance_part():
    deployment = {**DEPLOYMENT, "factChecksEnabled": False}
    settings, types = rules.effective_guidance_settings(
        deployment, meeting_with(Checks=["fact_check", "lookup"])
    )
    assert types is None and settings == deployment
