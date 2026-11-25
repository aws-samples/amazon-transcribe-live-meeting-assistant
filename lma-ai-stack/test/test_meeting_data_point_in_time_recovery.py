# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""The point-in-time recovery window of the tables that hold meeting content (no AWS).

`MeetingDataPointInTimeRecoveryDays` (lma-main.yaml, passed to the AI stack)
sets the DynamoDB point-in-time recovery of every table that holds meeting
content: 0 turns it off, 1 to 35 keeps it on with that recovery period. Every
other table keeps a 35-day window. See docs/privacy-and-data-handling.md and
docs/transcript-deletion.md.

Checked statically:

- every `AWS::DynamoDB::Table` in the repository's templates is classified here,
  either as a meeting content table or as a table that keeps 35 days, so a new
  table cannot be added without deciding which it is;
- the parameter is declared the same way in both templates, shown in the
  console's Retention group with a label, and passed to the AI stack;
- each content table's specification, evaluated for 0, 1, 7 and 35, is off at 0
  and on with that recovery period otherwise, and none of them sets point-in-time
  recovery to a fixed value;
- each other table keeps point-in-time recovery on, with the 35-day default.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from ai_stack_templates import TEMPLATE, load

REPO_ROOT = Path(__file__).resolve().parents[2]
MAIN = REPO_ROOT / "lma-main.yaml"
PARAMETER = "MeetingDataPointInTimeRecoveryDays"
CONDITION = "HasMeetingDataPointInTimeRecovery"
TABLE_TYPE = "AWS::DynamoDB::Table"

#: Tables that hold meeting content, all in the AI stack, and what each holds.
CONTENT_TABLES = {
    # Meeting records, transcript segments, summaries, consent records, chat lines
    # and attachment items, key-frame items, notifications, whiteboards, usage.
    "EventSourcingTable",
    # Meeting name, platform, meeting ID and password, schedule, agenda.
    "VirtualParticipantTable",
    # Each launched Virtual Participant's launch configuration, with the meeting's
    # ID, name and time, for about nine hours.
    "VPTaskRegistry",
    # The troubleshooting agent's answers about a meeting, which can quote it.
    "TroubleshootingJobsTable",
    # Voiceprints; a withdrawn enrollment must not stay restorable.
    "VoiceEnrollmentTable",
}

#: Tables that keep the 35-day window, by template, and why.
OTHER_TABLES = {
    "lma-ai-stack/deployment/lma-ai-stack.yaml": {
        # Append-only audit trail of consent given and withdrawn; nothing in LMA
        # deletes from it, and no voiceprint or meeting content is in it.
        "VoiceConsentLogTable",
        # Deployment configuration and its profiles.
        "ConfigurationTable",
        # Account-level MCP server, skill and API key (hashed) definitions.
        "MCPServersTable",
        "MCPApiKeysTable",
        "SkillsTable",
        # Short-lived OAuth flow state.
        "OAuthStateTable",
        # CSS selectors of meeting-platform pages.
        "DomSelectorCache",
    },
    "lma-asr-microvm-stack/template.yaml": {"AsrConfigTable"},
    # Each user's last password sign-in time, for access reviews; no meeting content.
    "lma-cognito-stack/deployment/lma-cognito-stack.yaml": {"UserSignInTable"},
    "lma-chat-button-config-stack/deployment/chat-button-config.yaml": {"ChatButtonConfigTable"},
    "lma-llm-template-setup-stack/deployment/llm-template-setup.yaml": {"LLMPromptTemplateTable"},
    "lma-nova-sonic-config-stack/deployment/nova-sonic-config.yaml": {"NovaSonicConfigTable"},
}

_SKIP_DIRS = {"node_modules", ".git", ".claude", ".aws-sam", "build", "dist", ".venv", "cdk.out"}


def _template_paths() -> list[Path]:
    paths = []
    for suffix in ("*.yaml", "*.yml"):
        for path in REPO_ROOT.rglob(suffix):
            parts = set(path.relative_to(REPO_ROOT).parts)
            if parts & _SKIP_DIRS or any(p.startswith(".venv") for p in parts):
                continue
            if TABLE_TYPE in path.read_text(encoding="utf-8", errors="ignore"):
                paths.append(path)
    return sorted(paths)


def _tables() -> dict[tuple[str, str], dict]:
    found = {}
    for path in _template_paths():
        resources = (load(path) or {}).get("Resources") or {}
        for logical_id, resource in resources.items():
            if isinstance(resource, dict) and resource.get("Type") == TABLE_TYPE:
                found[(path.relative_to(REPO_ROOT).as_posix(), logical_id)] = resource
    return found


@pytest.fixture(scope="module")
def tables() -> dict[tuple[str, str], dict]:
    return _tables()


@pytest.fixture(scope="module")
def ai_stack() -> dict:
    return load(TEMPLATE)


@pytest.fixture(scope="module")
def main() -> dict:
    return load(MAIN)


def _evaluate(value, days: str, conditions: dict):  # noqa: ANN001,ANN202
    """Resolve the parameter, the condition and `Fn::If` in a property value."""
    if isinstance(value, list):
        return [_evaluate(v, days, conditions) for v in value]
    if not isinstance(value, dict):
        return value
    if value == {"Ref": PARAMETER}:
        return int(days)
    if "Fn::If" in value:
        name, when_true, when_false = value["Fn::If"]
        return _evaluate(
            when_true if _condition(name, days, conditions) else when_false, days, conditions
        )
    return {k: _evaluate(v, days, conditions) for k, v in value.items()}


def _condition(name: str, days: str, conditions: dict) -> bool:
    assert name == CONDITION, f"point-in-time recovery depends on an unexpected condition {name!r}"
    expression = conditions[name]
    assert expression == {"Fn::Not": [{"Fn::Equals": [{"Ref": PARAMETER}, "0"]}]}, expression
    return days != "0"


def test_every_table_is_classified(tables):
    classified = {(TEMPLATE.relative_to(REPO_ROOT).as_posix(), t) for t in CONTENT_TABLES}
    classified |= {(path, t) for path, ids in OTHER_TABLES.items() for t in ids}
    assert set(tables) == classified, (
        "classify each table as meeting content (CONTENT_TABLES) or as keeping 35 days "
        f"(OTHER_TABLES): unclassified {sorted(set(tables) - classified)}, "
        f"no longer declared {sorted(classified - set(tables))}"
    )


@pytest.mark.parametrize("which", ["main", "ai_stack"])
def test_parameter_allows_only_whole_days_from_0_to_35_defaulting_to_35(which, request):
    template = request.getfixturevalue(which)
    parameter = template["Parameters"][PARAMETER]
    assert parameter["Type"] == "Number"
    assert parameter["MinValue"] == 0
    assert parameter["MaxValue"] == 35
    assert parameter["Default"] == 35
    assert [int(v) for v in parameter["AllowedValues"]] == list(range(36))


def test_main_stack_passes_the_parameter_to_the_ai_stack(main):
    assert main["Resources"]["AISTACK"]["Properties"]["Parameters"][PARAMETER] == {"Ref": PARAMETER}


def test_parameter_is_in_the_retention_group_with_a_label(main):
    interface = main["Metadata"]["AWS::CloudFormation::Interface"]
    groups = {g["Label"]["default"]: g["Parameters"] for g in interface["ParameterGroups"]}
    assert PARAMETER in groups["Retention Settings"]
    assert interface["ParameterLabels"][PARAMETER]["default"]


@pytest.mark.parametrize("table", sorted(CONTENT_TABLES))
@pytest.mark.parametrize("days", ["0", "1", "7", "35"])
def test_content_table_point_in_time_recovery_follows_the_parameter(ai_stack, table, days):
    spec = ai_stack["Resources"][table]["Properties"]["PointInTimeRecoverySpecification"]
    resolved = _evaluate(spec, days, ai_stack["Conditions"])
    if days == "0":
        assert resolved == {"PointInTimeRecoveryEnabled": False}
    else:
        assert resolved == {"PointInTimeRecoveryEnabled": True, "RecoveryPeriodInDays": int(days)}


@pytest.mark.parametrize("table", sorted(CONTENT_TABLES))
def test_content_table_does_not_set_point_in_time_recovery_to_a_fixed_value(ai_stack, table):
    spec = ai_stack["Resources"][table]["Properties"]["PointInTimeRecoverySpecification"]
    assert set(spec) == {"Fn::If"} and spec["Fn::If"][0] == CONDITION, spec
    when_on = spec["Fn::If"][1]
    assert when_on["RecoveryPeriodInDays"] == {"Ref": PARAMETER}


def test_other_tables_keep_a_35_day_window(tables):
    content = {(TEMPLATE.relative_to(REPO_ROOT).as_posix(), t) for t in CONTENT_TABLES}
    for key, resource in tables.items():
        if key in content:
            continue
        spec = resource["Properties"].get("PointInTimeRecoverySpecification")
        assert spec is not None, f"{key} has no point-in-time recovery"
        assert spec.get("PointInTimeRecoveryEnabled") is True, f"{key}: {spec}"
        assert spec.get("RecoveryPeriodInDays", 35) == 35, f"{key}: {spec}"
