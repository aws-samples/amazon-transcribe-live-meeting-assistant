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
Configuration page can override -- or, for a setting whose parameter is also its
upper limit (``maximumIsDefault``, the retention settings), that the page can
shorten it and not lengthen it; and every function and task that gives a record
an expiry reads the retention with the parameter's variable set, the table named,
the layer attached and read access to the table.

Run via `make test-ai-stack` or pytest directly.
"""

from __future__ import annotations

import ast
import json
import re
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


def _is_limit(entry) -> bool:
    return entry.setting in schema.limited_settings(schema.section_schema(entry.section))


@pytest.mark.parametrize("entry", ENTRIES, ids=_ids)
def test_the_console_description_says_it_is_a_default(entry) -> None:
    description = " ".join(MAIN["Parameters"][entry.parameter]["Description"].split())
    if _is_limit(entry):
        assert (
            "This is the default and the upper limit; an Admin can set a shorter period at run "
            "time on the Configuration page"
        ) in description
        return
    assert "This is the default; an Admin can change it at run time on the Configuration page" in (
        description
    )


def test_the_retention_settings_are_limits() -> None:
    limits = {entry.parameter for entry in ENTRIES if _is_limit(entry)}
    assert limits == {"MeetingRecordExpirationInDays", "TranscriptionExpirationInDays"}


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


# ── the TypeScript readers ──────────────────────────────────────────────────

TRANSCRIBER_TEMPLATE = (
    REPO_ROOT / "lma-websocket-transcriber-stack" / "deployment" / "lma-websocket-transcriber.yaml"
)
TRANSCRIBER_SETTINGS = (
    REPO_ROOT
    / "lma-websocket-transcriber-stack"
    / "source"
    / "app"
    / "src"
    / "calleventdata"
    / "session-settings.ts"
)

#: Each TypeScript settings table, and the section its entries belong to.
TYPESCRIPT_TABLES = {
    (TRANSCRIBER_SETTINGS, "TRANSCRIPTION_SETTINGS"): "transcription",
    (TRANSCRIBER_SETTINGS, "RECORDING_SETTINGS"): "recording",
    (TRANSCRIBER_SETTINGS, "RETENTION_SETTINGS"): "retention",
}

#: A table entry, one per line: `name: { envVar: 'NAME', type: 'kind' },`.
TABLE_ENTRY = re.compile(r"^\s+(\w+): \{ envVar: '(\w+)', type: '(\w+)' \},$")

#: Environment variables the template sets through a condition rather than as
#: the parameter itself, and why that is still the parameter's value.
CONDITIONED_VARIABLES = {
    # !If [ShouldEnableContentRedaction, "true", "false"]: the flag, already
    # combined with the language check the reader also makes.
    "IS_CONTENT_REDACTION_ENABLED",
}


def _typescript_entries() -> List[Tuple[str, str, str, str]]:
    """(section, setting, env_var, type) of every TypeScript table entry."""
    found = []
    for (path, table), section in TYPESCRIPT_TABLES.items():
        text = path.read_text(encoding="utf-8")
        start = text.index(f"export const {table}")
        end = text.index("};", start)
        for line in text[start:end].splitlines():
            match = TABLE_ENTRY.match(line)
            if match:
                found.append((section, *match.groups()))
    return found


TYPESCRIPT_ENTRIES = _typescript_entries()


def _transcriber_environment() -> Dict[str, Any]:
    template = _load(TRANSCRIBER_TEMPLATE)
    container = template["Resources"]["TranscriberWebsocketTaskDef"]["Properties"]["ContainerDefinitions"][0]
    return {variable["Name"]: variable["Value"] for variable in container["Environment"]}


def test_the_typescript_tables_are_found() -> None:
    assert {section for section, *_ in TYPESCRIPT_ENTRIES} == set(TYPESCRIPT_TABLES.values())


@pytest.mark.parametrize("section, setting, env_var, value_type", TYPESCRIPT_ENTRIES)
def test_a_typescript_reader_names_a_mapped_setting_of_the_same_type(
    section, setting, env_var, value_type
) -> None:
    entry = _by_setting().get((section, setting))
    assert entry is not None, f"the transcriber reads {section} › {setting}, which no parameter backs"
    assert entry.value_type == value_type


@pytest.mark.parametrize("section, setting, env_var, value_type", TYPESCRIPT_ENTRIES)
def test_the_transcriber_task_carries_the_fallback_variable(section, setting, env_var, value_type) -> None:
    entry = _by_setting()[(section, setting)]
    environment = _transcriber_environment()
    assert env_var in environment, f"the transcriber task sets no {env_var}"
    if env_var not in CONDITIONED_VARIABLES:
        assert environment[env_var] == {"Ref": entry.parameter}
    passed = MAIN["Resources"]["WEBSOCKETTRANSCRIBERSTACK"]["Properties"]["Parameters"]
    assert passed.get(entry.parameter) == {"Ref": entry.parameter}


def test_the_transcriber_task_can_read_the_configuration_table() -> None:
    environment = _transcriber_environment()
    assert environment["CONFIGURATION_TABLE_NAME"] == {"Ref": "ConfigurationTableName"}
    passed = MAIN["Resources"]["WEBSOCKETTRANSCRIBERSTACK"]["Properties"]["Parameters"]
    assert passed["ConfigurationTableName"] == {"Fn::GetAtt": "AISTACK.Outputs.ConfigurationTableName"}
    # The grant is a policy resource attached to the task role.
    resources = _load(TRANSCRIBER_TEMPLATE)["Resources"]
    attached = [
        resource
        for resource in resources.values()
        if resource.get("Type") == "AWS::IAM::Policy"
        and {"Ref": "TranscriberWebsocketTaskDefTaskRole"} in resource["Properties"].get("Roles", [])
    ]
    rendered = json.dumps(attached, default=str)
    assert "dynamodb:GetItem" in rendered and "${ConfigurationTableName}" in rendered


def _module_strings(path: Path) -> Dict[str, Any]:
    """Module-level NAME = "text" (or a number) assignments of a source file."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = {}
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, (str, int))
            and not isinstance(node.value.value, bool)
        ):
            found[node.targets[0].id] = node.value.value
    return found


def _named_defaults() -> List[Any]:
    """(file, parameter, the fallback a read_setting call names, its value)."""
    found = []
    sources = _sources(AI_FUNCTIONS) + _sources(MEETING_ASSIST_SRC)
    for path in sources:
        strings = _module_strings(path)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
            keywords = {kw.arg: kw.value for kw in node.keywords}
            default = keywords.get("default")
            if name != "read_setting" or not isinstance(default, ast.Name) or default.id not in strings:
                continue
            entry = _by_setting()[(_section_name(node.args[0]), node.args[1].value)]
            found.append(
                pytest.param(
                    entry.parameter,
                    strings[default.id],
                    id=f"{path.parent.name}/{path.name}:{default.id}",
                )
            )
    return found


NAMED_DEFAULTS = _named_defaults()


def test_the_named_fallbacks_are_found() -> None:
    assert len(NAMED_DEFAULTS) >= 6


@pytest.mark.parametrize("parameter, fallback", NAMED_DEFAULTS)
def test_a_readers_last_resort_is_the_parameters_own_default(parameter, fallback) -> None:
    """A reader that finds neither the page's value nor its variable uses this.

    It is a copy of the template's default, so it is pinned to it: a release that
    changes the default and not the copy would leave a reader on the old one.
    """
    # Compared as text: a Number parameter's default is read from YAML as a number.
    assert str(fallback) == str(MAIN["Parameters"][parameter]["Default"])


# ── the Virtual Participant ─────────────────────────────────────────────────

VP_BACKEND = REPO_ROOT / "lma-virtual-participant-stack" / "backend" / "src"
VP_TEMPLATE = REPO_ROOT / "lma-virtual-participant-stack" / "template.yaml"

#: A row of the Virtual Participant's table, one per line.
VP_TABLE_ENTRY = re.compile(
    r"^\s+\{ section: '(\w+)', setting: '(\w+)', envVar: '(\w+)', type: '(\w+)' \},$"
)

#: The modules that read these variables when they are imported, and so must
#: import stack-settings.js before anything else.
VP_IMPORT_TIME_READERS = ("details.ts", "voice-assistant.ts", "simli-avatar.ts", "scribe.ts")


def _vp_entries(table: str = "VIRTUAL_PARTICIPANT_SETTINGS") -> List[Tuple[str, str, str, str]]:
    text = (VP_BACKEND / "stack-settings.ts").read_text(encoding="utf-8")
    start = text.index(f"export const {table}:")
    end = text.index("];", start)
    return [m.groups() for m in map(VP_TABLE_ENTRY.match, text[start:end].splitlines()) if m]


VP_ENTRIES = _vp_entries()
#: The settings whose parameter is their limit, applied as the smaller value.
VP_LIMITED_ENTRIES = _vp_entries("VIRTUAL_PARTICIPANT_LIMITED_SETTINGS")


def _vp_environment() -> Dict[str, Any]:
    container = _load(VP_TEMPLATE)["Resources"]["TaskDefinition"]["Properties"]["ContainerDefinitions"][0]
    return {variable["Name"]: variable["Value"] for variable in container["Environment"]}


def test_the_virtual_participant_table_is_found() -> None:
    assert len(VP_ENTRIES) >= 13
    assert {section for section, *_ in VP_ENTRIES} == {"transcription", "recording", "virtual_participant"}


def test_the_virtual_participant_applies_the_meeting_retention_as_a_limit() -> None:
    assert VP_LIMITED_ENTRIES == [
        ("retention", "meetingRecordExpirationInDays", "METERING_EXPIRATION_DAYS", "number")
    ]
    assert all(_is_limit(_by_setting()[(s, n)]) for s, n, *_ in VP_LIMITED_ENTRIES)


@pytest.mark.parametrize("section, setting, env_var, value_type", VP_ENTRIES + VP_LIMITED_ENTRIES)
def test_a_virtual_participant_setting_is_a_mapped_one_of_the_same_type(
    section, setting, env_var, value_type
) -> None:
    entry = _by_setting().get((section, setting))
    assert entry is not None, f"the Virtual Participant applies {section} › {setting}, which no parameter backs"
    assert entry.value_type == value_type


@pytest.mark.parametrize("section, setting, env_var, value_type", VP_ENTRIES + VP_LIMITED_ENTRIES)
def test_the_virtual_participant_task_sets_the_variable_from_the_same_parameter(
    section, setting, env_var, value_type
) -> None:
    entry = _by_setting()[(section, setting)]
    environment = _vp_environment()
    assert env_var in environment, f"the Virtual Participant task sets no {env_var}"
    # Either form passes the parameter's own value through.
    assert environment[env_var] in ({"Ref": entry.parameter}, {"Fn::Sub": f"${{{entry.parameter}}}"})
    passed = MAIN["Resources"]["VIRTUALPARTICIPANTSTACK"]["Properties"]["Parameters"]
    assert passed.get(entry.parameter) == {"Ref": entry.parameter}


def test_every_virtual_participant_section_setting_is_applied() -> None:
    """A setting on the page that nothing applies would be a setting that does nothing."""
    applied = {setting for section, setting, *_ in VP_ENTRIES if section == "virtual_participant"}
    declared = set(schema.section_schema("virtual_participant")["properties"])
    assert applied == declared


@pytest.mark.parametrize("module", VP_IMPORT_TIME_READERS)
def test_an_import_time_reader_imports_the_settings_first(module) -> None:
    text = (VP_BACKEND / module).read_text(encoding="utf-8")
    first_import = next(line for line in text.splitlines() if line.startswith("import "))
    assert first_import == "import './stack-settings.js';", (
        f"{module} reads the environment when it is imported, so its first import must be "
        f"stack-settings.js, which applies the Configuration page's values; it is {first_import!r}"
    )


def test_the_virtual_participant_task_can_read_the_configuration_table() -> None:
    environment = _vp_environment()
    assert environment["CONFIGURATION_TABLE_NAME"] == {"Ref": "ConfigurationTableName"}
    rendered = json.dumps(_load(VP_TEMPLATE)["Resources"]["TaskRole"]["Properties"], default=str)
    assert "dynamodb:GetItem" in rendered and "ConfigurationTableName" in rendered


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


# ── the writers that give a record an expiry ────────────────────────────────

#: The environment variable each kind of writer reads its retention from, the
#: parameter it must hold, and what in a function's source marks it as a writer.
#: The readers are in the shared layers (eventprocessor_utils, lma_metering), so
#: the scan of read_setting calls above does not see them.
RETENTION_VARIABLES = {
    "MEETING_RECORD_EXPIRATION_IN_DAYS": ("MeetingRecordExpirationInDays", "get_meeting_ttl"),
    "TRANSCRIPTION_RECORD_EXPIRATION_IN_DAYS": (
        "TranscriptionExpirationInDays",
        "get_transcription_ttl",
    ),
    "METERING_EXPIRATION_DAYS": ("MeetingRecordExpirationInDays", "MeteringWriter"),
}


def _retention_writers() -> List[Any]:
    """(function, resource, variable) for each AI-stack function that writes such records."""
    found = []
    for name, resource in AI["Resources"].items():
        if resource.get("Type") != "AWS::Serverless::Function":
            continue
        code = resource["Properties"].get("CodeUri", "")
        if not isinstance(code, str) or "lambda_functions/" not in code:
            continue
        directory = AI_FUNCTIONS / code.rstrip("/").split("lambda_functions/")[1]
        text = "\n".join(p.read_text(encoding="utf-8") for p in _sources(directory))
        for variable, (_parameter, marker) in RETENTION_VARIABLES.items():
            if re.search(rf"\b{marker}\b", text):
                found.append(pytest.param(name, resource, variable, id=f"{name}:{variable}"))
    return found


RETENTION_WRITERS = _retention_writers()


def test_the_retention_writers_are_found() -> None:
    names = {param.values[0] for param in RETENTION_WRITERS}
    assert {
        "CallEventProcessorFunction",
        "AsyncAgentAssistOrchestrator",
        "AsyncTranscriptSummaryOrchestrator",
        "UploadMeetingFinalizerFunction",
    } <= names


@pytest.mark.parametrize("function, resource, variable", RETENTION_WRITERS)
def test_a_retention_writer_carries_the_parameter_and_can_read_the_section(
    function, resource, variable
) -> None:
    """Without the variable a writer falls back to 90 days whatever the stack says;
    without the table, the layer or the grant it never sees a shorter period."""
    parameter, _marker = RETENTION_VARIABLES[variable]
    properties = resource["Properties"]
    variables = properties.get("Environment", {}).get("Variables", {})
    assert variables.get(variable) == {"Ref": parameter}, (
        f"{function} writes records that expire, so it must set {variable} from {parameter}"
    )
    assert variables.get("CONFIGURATION_TABLE_NAME") == {"Ref": "ConfigurationTable"}
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
