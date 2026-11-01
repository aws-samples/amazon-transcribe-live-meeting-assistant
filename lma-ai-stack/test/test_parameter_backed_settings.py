# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""The stack parameters that are defaults of Configuration page settings (no AWS).

`lma_config/parameter_defaults.json` names each stack parameter whose value is
now the default of a runtime setting. Three places have to agree with it, and a
mismatch in any of them is silent at deploy time -- a setting whose default
never reaches the configuration reads as "not set" and the readers quietly use
their environment variable, which looks exactly like a working deployment:

* **lma-main.yaml passes the parameter to the AI stack**, and the AI stack hands
  it to the configuration seeder as `ParameterDefaults.<Parameter>`, so every
  stack update that changes it re-runs the seeder;
* **the value is one the section accepts**: the parameter's default and every
  one of its allowed values are valid against the section schema, and where the
  schema lists choices they are exactly the parameter's;
* **every reader is wired**: each `read_setting` call names a mapped setting, and
  each function built from that code has the fallback environment variable set
  from the same parameter, the configuration table's name, the configuration
  layer, and read access to the table.

Also asserted: the parameter's console description says it is a default that the
Configuration page can override.

Run via `make test-ai-stack` or pytest directly.
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path
from typing import Any, Dict, Iterator, List, Tuple

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
MAIN_TEMPLATE = REPO_ROOT / "lma-main.yaml"
AI_TEMPLATE = REPO_ROOT / "lma-ai-stack" / "deployment" / "lma-ai-stack.yaml"
MEETING_ASSIST_TEMPLATE = REPO_ROOT / "lma-meetingassist-setup-stack" / "template.yaml"
LAYER = REPO_ROOT / "lma-ai-stack" / "source" / "lambda_layers" / "configuration_layer"
AI_FUNCTIONS = REPO_ROOT / "lma-ai-stack" / "source" / "lambda_functions"
MEETING_ASSIST_SRC = REPO_ROOT / "lma-meetingassist-setup-stack" / "src"

if str(LAYER) not in sys.path:
    sys.path.insert(0, str(LAYER))

from lma_config import parameters, schema, sections  # noqa: E402
from lma_config.errors import InvalidConfiguration  # noqa: E402


class _CfnLoader(yaml.SafeLoader):
    """YAML loader that tolerates CloudFormation short-form intrinsics."""


def _intrinsic(loader, tag_suffix, node):  # noqa: ANN001, ANN202
    name = f"Fn::{tag_suffix}" if tag_suffix != "Ref" else "Ref"
    if isinstance(node, yaml.ScalarNode):
        return {name: loader.construct_scalar(node)}
    if isinstance(node, yaml.SequenceNode):
        return {name: loader.construct_sequence(node, deep=True)}
    return {name: loader.construct_mapping(node, deep=True)}


_CfnLoader.add_multi_constructor("!", _intrinsic)


def _load(path: Path) -> Dict[str, Any]:
    return yaml.load(path.read_text(encoding="utf-8"), Loader=_CfnLoader)


MAIN = _load(MAIN_TEMPLATE)
AI = _load(AI_TEMPLATE)
MEETING_ASSIST = _load(MEETING_ASSIST_TEMPLATE)
ENTRIES = parameters.parameter_defaults()


def _ids(entry):
    return entry.parameter


# ── the seeder is given every parameter ─────────────────────────────────────


@pytest.mark.parametrize("entry", ENTRIES, ids=_ids)
def test_the_parameter_is_declared_by_the_main_template(entry) -> None:
    assert entry.parameter in MAIN["Parameters"]


@pytest.mark.parametrize("entry", ENTRIES, ids=_ids)
def test_the_main_template_passes_the_parameter_to_the_ai_stack(entry) -> None:
    passed = MAIN["Resources"]["AISTACK"]["Properties"]["Parameters"]
    assert passed.get(entry.parameter) == {"Ref": entry.parameter}
    assert entry.parameter in AI["Parameters"]


@pytest.mark.parametrize("entry", ENTRIES, ids=_ids)
def test_the_seeder_is_given_the_parameter(entry) -> None:
    given = AI["Resources"]["SeedConfiguration"]["Properties"]["ParameterDefaults"]
    assert given.get(entry.parameter) == {"Ref": entry.parameter}, (
        f"SeedConfiguration.ParameterDefaults does not pass {entry.parameter}, so the "
        f"{entry.section} › {entry.setting} default never reaches the configuration"
    )


def test_the_seeder_is_given_nothing_the_file_does_not_name() -> None:
    given = AI["Resources"]["SeedConfiguration"]["Properties"]["ParameterDefaults"]
    assert set(given) == {entry.parameter for entry in ENTRIES}


# ── the value is one the section accepts ────────────────────────────────────


def _accepts(entry, value) -> bool:
    try:
        schema.validate_section(
            entry.section, {entry.setting: parameters.coerce(entry.value_type, value)}
        )
    except (InvalidConfiguration, ValueError):
        return False
    return True


@pytest.mark.parametrize("entry", ENTRIES, ids=_ids)
def test_the_parameters_default_is_a_value_the_section_accepts(entry) -> None:
    default = MAIN["Parameters"][entry.parameter].get("Default")
    assert _accepts(entry, default), f"{entry.parameter}'s default {default!r} would not be seeded"


@pytest.mark.parametrize("entry", ENTRIES, ids=_ids)
def test_every_allowed_value_is_one_the_section_accepts(entry) -> None:
    allowed = MAIN["Parameters"][entry.parameter].get("AllowedValues", [])
    refused = [value for value in allowed if not _accepts(entry, value)]
    assert not refused, f"{entry.parameter} allows {refused}, which the section would not seed"


@pytest.mark.parametrize("entry", ENTRIES, ids=_ids)
def test_a_listed_choice_is_exactly_the_parameters_allowed_values(entry) -> None:
    """The page offers what a stack update could set, and nothing more.

    A model the parameter lists but the page does not would be seeded and then
    refused on the next save of the section; one the page lists but the
    parameter does not has been through none of the parameter's checks.
    """
    declared = schema.section_schema(entry.section)["properties"][entry.setting]
    allowed = MAIN["Parameters"][entry.parameter].get("AllowedValues")
    if "enum" not in declared or allowed is None:
        pytest.skip("neither side lists choices")
    assert declared["enum"] == allowed


@pytest.mark.parametrize("entry", ENTRIES, ids=_ids)
def test_the_console_description_says_it_is_a_default(entry) -> None:
    description = " ".join(MAIN["Parameters"][entry.parameter]["Description"].split())
    assert "This is the default; an Admin can change it at run time on the Configuration page" in (
        description
    )


# ── every reader is wired ───────────────────────────────────────────────────


def _section_name(node: ast.AST) -> str:
    """`LANGUAGE_MODELS.name` -> "language_models"; a literal stays itself."""
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Attribute) and node.attr == "name" and isinstance(node.value, ast.Name):
        return getattr(sections, node.value.id).name
    raise AssertionError(f"cannot resolve the section of a read_setting call: {ast.dump(node)}")


def _reads(path: Path) -> Iterator[Tuple[str, str, str]]:
    """(section, setting, env_var) of every read_setting call in a source file."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        if name != "read_setting":
            continue
        keywords = {kw.arg: kw.value for kw in node.keywords}
        yield (
            _section_name(node.args[0]),
            node.args[1].value,
            keywords["env_var"].value,
        )


def _sources(directory: Path) -> List[Path]:
    return [p for p in directory.rglob("*.py") if not p.name.startswith("test_")]


def _ai_readers() -> List[Tuple[str, Dict[str, Any], Tuple[str, str, str]]]:
    found = []
    for name, resource in AI["Resources"].items():
        if resource.get("Type") != "AWS::Serverless::Function":
            continue
        code = resource["Properties"].get("CodeUri", "")
        if not isinstance(code, str) or "lambda_functions/" not in code:
            continue
        directory = AI_FUNCTIONS / code.rstrip("/").split("lambda_functions/")[1]
        for source in _sources(directory):
            for read in _reads(source):
                found.append((name, resource, read))
    return found


def _meeting_assist_readers() -> List[Tuple[str, Dict[str, Any], Tuple[str, str, str]]]:
    found = []
    for name, resource in MEETING_ASSIST["Resources"].items():
        if resource.get("Type") != "AWS::Lambda::Function":
            continue
        module = resource["Properties"]["Handler"].split(".")[0]
        source = MEETING_ASSIST_SRC / f"{module}.py"
        for read in _reads(source):
            found.append((name, resource, read))
    return found


def _params(readers):
    return [
        pytest.param(name, resource, read, id=f"{name}:{read[1]}")
        for name, resource, read in readers
    ]


AI_READERS = _params(_ai_readers())
MEETING_ASSIST_READERS = _params(_meeting_assist_readers())


def _role(template: Dict[str, Any], properties: Dict[str, Any]) -> Dict[str, Any]:
    """The role a function's Role names with !GetAtt <Role>.Arn."""
    target = properties["Role"]["Fn::GetAtt"]
    logical_id = target.split(".")[0] if isinstance(target, str) else target[0]
    return template["Resources"][logical_id]


def test_the_readers_are_found() -> None:
    """Guards the scan: a renamed function would leave the tests below with nothing."""
    assert len(AI_READERS) >= 7
    assert len(MEETING_ASSIST_READERS) >= 3


def _by_setting():
    return {(e.section, e.setting): e for e in ENTRIES}


@pytest.mark.parametrize("function, resource, read", AI_READERS + MEETING_ASSIST_READERS)
def test_every_read_names_a_mapped_setting(function, resource, read) -> None:
    section, setting, _env_var = read
    assert (section, setting) in _by_setting(), (
        f"{function} reads {section} › {setting}, which no parameter backs"
    )


@pytest.mark.parametrize("function, resource, read", AI_READERS + MEETING_ASSIST_READERS)
def test_the_fallback_variable_is_the_same_parameter(function, resource, read) -> None:
    section, setting, env_var = read
    entry = _by_setting()[(section, setting)]
    variables = resource["Properties"].get("Environment", {}).get("Variables", {})
    assert variables.get(env_var) == {"Ref": entry.parameter}, (
        f"{function} falls back to {env_var}, which must be set from {entry.parameter}"
    )


@pytest.mark.parametrize("function, resource, read", AI_READERS)
def test_an_ai_stack_reader_can_reach_the_table(function, resource, read) -> None:
    properties = resource["Properties"]
    assert properties["Environment"]["Variables"].get("CONFIGURATION_TABLE_NAME") == {
        "Ref": "ConfigurationTable"
    }
    assert {"Ref": "ConfigurationPythonLayer"} in properties.get("Layers", [])
    policies = (
        _role(AI, properties)["Properties"]["Policies"]
        if "Role" in properties
        else properties.get("Policies")
    )
    rendered = json.dumps(policies, default=str)
    assert "dynamodb:GetItem" in rendered and "ConfigurationTable" in rendered, (
        f"{function}'s role cannot read the configuration table"
    )


@pytest.mark.parametrize("function, resource, read", MEETING_ASSIST_READERS)
def test_a_meeting_assist_reader_can_reach_the_table(function, resource, read) -> None:
    """Behind HasConfiguration, which lma-main.yaml's two inputs always satisfy.

    A standalone deployment of the template supplies neither, and its functions
    then read the parameters' environment variables (configuration_settings.py).
    """
    properties = resource["Properties"]
    assert properties["Environment"]["Variables"].get("CONFIGURATION_TABLE_NAME") == {
        "Fn::If": ["HasConfiguration", {"Ref": "ConfigurationTableName"}, ""]
    }
    assert {
        "Fn::If": ["HasConfiguration", {"Ref": "ConfigurationLayerArn"}, {"Ref": "AWS::NoValue"}]
    } in properties["Layers"]
    rendered = json.dumps(_role(MEETING_ASSIST, properties)["Properties"]["Policies"], default=str)
    assert "dynamodb:GetItem" in rendered and "${ConfigurationTableName}" in rendered


def test_the_meeting_assist_configuration_is_read_only_when_both_inputs_are_given() -> None:
    condition = MEETING_ASSIST["Conditions"]["HasConfiguration"]
    rendered = json.dumps(condition)
    assert "ConfigurationTableName" in rendered and "ConfigurationLayerArn" in rendered
    for name in ("ConfigurationTableName", "ConfigurationLayerArn"):
        assert MEETING_ASSIST["Parameters"][name]["Default"] == ""


@pytest.mark.parametrize("entry", ENTRIES, ids=_ids)
def test_the_configuration_guide_names_the_parameter_a_default(entry) -> None:
    guide = (REPO_ROOT / "docs" / "configuration-guide.md").read_text(encoding="utf-8")
    assert f"| `{entry.parameter}` |" in guide, (
        f"docs/configuration-guide.md's table of settings whose default is a stack "
        f"parameter has no row for {entry.parameter}"
    )


def test_the_main_template_gives_the_meeting_assist_stack_the_table_and_the_layer() -> None:
    passed = MAIN["Resources"]["MEETINGASSISTSETUP"]["Properties"]["Parameters"]
    assert passed["ConfigurationTableName"] == {
        "Fn::GetAtt": "AISTACK.Outputs.ConfigurationTableName"
    }
    assert passed["ConfigurationLayerArn"] == {
        "Fn::GetAtt": "AISTACK.Outputs.ConfigurationPythonLayerArn"
    }
    assert "ConfigurationPythonLayerArn" in AI["Outputs"]
