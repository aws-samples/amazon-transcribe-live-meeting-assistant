# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Every log destination in every deployed template, checked statically (no AWS).

A Lambda function with no `LoggingConfig` still logs: Lambda creates
`/aws/lambda/<function-name>` on the first invocation, with no retention and the
AWS-owned key, and CloudFormation neither owns nor deletes it. The same is true
of a declared log group that nothing points at — the group sits empty while the
function logs somewhere else. Both are invisible until somebody goes looking for
a log, or reads a bill, so this file asserts the wiring instead.

The five rules, each as its own test so a failure names which one broke:

1. every `AWS::Lambda::Function` and `AWS::Serverless::Function` sets
   `LoggingConfig.LogGroup` to a `!Ref` of a log group declared in the same
   template;
2. every `AWS::Logs::LogGroup` sets a non-null `RetentionInDays`;
3. no `LogGroupName` is derived from a function resource, in any intrinsic form.
   `!Sub "/aws/lambda/${SomeFunction}"` resolves the function's generated
   physical name, which makes the group depend on the function. CloudFormation
   then creates the function first, and anything that invokes it in that window
   lets Lambda create the group — after which the stack's own group fails to
   create, because the name is taken;
4. a log group's `Condition` matches the `Condition` of the function that names
   it, so a conditional function cannot end up pointing at a group that was not
   created (or vice versa);
5. every declared log group is referenced by something.

Scoped to the templates `lma-main.yaml` actually instantiates, read from the
`CFN_TEMPLATES` list in the root `Makefile` so the two cannot drift. There is no
per-function exemption list: a new function that does not comply fails rule 1.

Run via `make test-ai-stack` or pytest directly.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
MAIN_TEMPLATE = REPO_ROOT / "lma-main.yaml"
MAKEFILE = REPO_ROOT / "Makefile"

FUNCTION_TYPES = ("AWS::Lambda::Function", "AWS::Serverless::Function")
LOG_GROUP_TYPE = "AWS::Logs::LogGroup"

# `CFN_TEMPLATES` is written with make variables for the longer directories.
MAKE_VARIABLES = {
    "$(AI_STACK_DIR)": "lma-ai-stack",
    "$(ASR_DIR)": "lma-asr-microvm-stack",
    "$(VP_DIR)": "lma-virtual-participant-stack",
    "$(WEBSOCKET_DIR)": "lma-websocket-transcriber-stack",
}

# Linted, but not instantiated by lma-main.yaml, so nothing in them reaches a
# deployment. They are exempt from the rules below as whole templates rather
# than per-resource, and `test_the_exempt_templates_are_still_not_deployed`
# keeps that honest: wiring either one into lma-main.yaml fails that test, which
# is the point at which its functions have to comply. The value of each entry is
# the string whose absence from lma-main.yaml is the evidence for the exemption.
TEMPLATES_NOT_DEPLOYED = {
    "lma-ai-stack/deployment/virtual-participant-enhancements.yaml": (
        "virtual-participant-enhancements"
    ),
    "lma-bedrockagent-stack/template.yaml": "lma-bedrockagent-stack",
}


class _CfnLoader(yaml.SafeLoader):
    """YAML loader that tolerates CloudFormation short-form intrinsics."""


def _intrinsic(loader: yaml.Loader, tag_suffix: str, node: yaml.Node):  # noqa: ANN202
    name = f"Fn::{tag_suffix}" if tag_suffix != "Ref" else "Ref"
    if isinstance(node, yaml.ScalarNode):
        return {name: loader.construct_scalar(node)}
    if isinstance(node, yaml.SequenceNode):
        return {name: loader.construct_sequence(node, deep=True)}
    return {name: loader.construct_mapping(node, deep=True)}


_CfnLoader.add_multi_constructor("!", _intrinsic)


def _cfn_templates() -> list[str]:
    """The `CFN_TEMPLATES` list from the root Makefile, as repo-relative paths."""
    body = MAKEFILE.read_text().split("CFN_TEMPLATES := \\", 1)[1]
    paths = []
    for line in body.splitlines():
        entry = line.strip().rstrip("\\").strip()
        if not entry:
            if paths:  # the list ends at the first blank line after it
                break
            continue  # the remainder of the `CFN_TEMPLATES := \` line itself
        for variable, directory in MAKE_VARIABLES.items():
            entry = entry.replace(variable, directory)
        paths.append(entry)
    return paths


ALL_TEMPLATES = _cfn_templates()
DEPLOYED_TEMPLATES = [path for path in ALL_TEMPLATES if path not in TEMPLATES_NOT_DEPLOYED]


def _load(path: str) -> dict:
    return yaml.load((REPO_ROOT / path).read_text(), Loader=_CfnLoader)


def _resources(path: str) -> dict:
    return _load(path).get("Resources", {}) or {}


def _of_type(resources: dict, *types: str) -> dict:
    return {k: v for k, v in resources.items() if v.get("Type") in types}


def _references(node, path: str = ""):
    """Yield (target, path) for every Ref / GetAtt / Sub variable in `node`."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "Ref" and isinstance(value, str):
                yield (value, path)
            elif key == "Fn::GetAtt":
                target = value[0] if isinstance(value, list) else str(value).split(".")[0]
                if isinstance(target, str):
                    yield (target, path)
            elif key == "Fn::Sub":
                body = value[0] if isinstance(value, list) else value
                if isinstance(body, str):
                    for variable in re.findall(r"\$\{([A-Za-z0-9:.]+)\}", body):
                        yield (variable.split(".")[0], path)
            yield from _references(value, f"{path}/{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _references(value, f"{path}[{index}]")


def _log_group_ref(function: dict) -> object:
    return (function.get("Properties", {}).get("LoggingConfig") or {}).get("LogGroup")


def _as_name(value: object) -> str | None:
    """The name a `LogGroupName` / `LoggingConfig.LogGroup` scalar spells out.

    `${...}` placeholders are left in place: both ends of the comparison are
    written against the same pseudo-parameters, so comparing the unexpanded
    strings is exact. Anything that is not a plain string or a single `Fn::Sub`
    of one has no name to compare and yields None.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and set(value) == {"Fn::Sub"}:
        body = value["Fn::Sub"]
        if isinstance(body, str):
            return body
    return None


def _destination(function: dict, log_groups: dict) -> str | None:
    """The logical id of the log group a function logs to, or None if it is not one.

    Two spellings are in use and both are accepted: `!Ref <group>`, and the
    group's name repeated verbatim (the older and still more common form in
    these templates). The second only counts when some declared group really
    carries that name — which is what makes a typo in it a failure here.
    """
    reference = _log_group_ref(function)
    if isinstance(reference, dict) and set(reference) == {"Ref"}:
        return reference["Ref"] if reference["Ref"] in log_groups else None
    wanted = _as_name(reference)
    if wanted is None:
        return None
    for logical_id, group in log_groups.items():
        if _as_name(group.get("Properties", {}).get("LogGroupName")) == wanted:
            return logical_id
    return None


# ── the template list itself ────────────────────────────────────────────────


def test_the_template_list_parses_at_the_expected_scale():
    """Guards every parametrized test below against collecting nothing."""
    assert len(ALL_TEMPLATES) >= 16, f"only found {len(ALL_TEMPLATES)} templates in CFN_TEMPLATES"
    missing = [path for path in ALL_TEMPLATES if not (REPO_ROOT / path).is_file()]
    assert not missing, f"CFN_TEMPLATES names templates that do not exist: {missing}"


def test_cfn_templates_covers_every_nested_stack_lma_main_deploys():
    """A nested stack missing from the list is linted by nothing and tested by nothing.

    Compared by stack directory rather than by file, because the S3 key in a
    `TemplateURL` is the published layout (`lma-cognito-stack/template.yaml`),
    not the repo layout (`lma-cognito-stack/deployment/lma-cognito-stack.yaml`).
    """
    urls = re.findall(
        r"TemplateURL:\s*\S*<ARTIFACT_PREFIX_TOKEN>/([^/\s]+)/", MAIN_TEMPLATE.read_text()
    )
    assert len(urls) >= 13, f"only found {len(urls)} nested stack TemplateURLs — regex stale?"
    listed = {path.split("/", 1)[0] for path in ALL_TEMPLATES}
    missing = sorted(set(urls) - listed)
    assert not missing, (
        f"lma-main.yaml deploys these stacks but CFN_TEMPLATES does not list their "
        f"templates, so they are neither linted nor covered here: {missing}"
    )


def test_the_exempt_templates_are_still_not_deployed():
    """The exemption in `TEMPLATES_NOT_DEPLOYED` holds only while that stays true."""
    main = MAIN_TEMPLATE.read_text()
    now_deployed = sorted(
        path for path, evidence in TEMPLATES_NOT_DEPLOYED.items() if evidence in main
    )
    assert not now_deployed, (
        f"these templates are exempt from the log-destination rules because "
        f"lma-main.yaml does not instantiate them, which is no longer true: "
        f"{now_deployed}. Remove them from TEMPLATES_NOT_DEPLOYED and wire their "
        f"functions to declared log groups."
    )


# ── the five rules ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("template", DEPLOYED_TEMPLATES)
def test_every_function_names_a_log_group_declared_in_the_same_template(template: str):
    """Rule 1. Without this, Lambda creates the group and sets its own defaults."""
    resources = _resources(template)
    log_groups = _of_type(resources, LOG_GROUP_TYPE)
    broken = []
    for name, function in _of_type(resources, *FUNCTION_TYPES).items():
        reference = _log_group_ref(function)
        if reference is None:
            broken.append(f"{name}: no LoggingConfig.LogGroup")
        elif _destination(function, log_groups) is None:
            broken.append(
                f"{name}: LoggingConfig.LogGroup ({reference!r}) is not a log group "
                f"declared here"
            )
    assert not broken, f"{template}: {broken}"


@pytest.mark.parametrize("template", DEPLOYED_TEMPLATES)
def test_every_log_group_sets_a_retention(template: str):
    """Rule 2. A log group with no `RetentionInDays` keeps its events forever."""
    without = [
        name
        for name, group in _of_type(_resources(template), LOG_GROUP_TYPE).items()
        if group.get("Properties", {}).get("RetentionInDays") is None
    ]
    assert not without, f"{template}: log groups with no RetentionInDays: {without}"


@pytest.mark.parametrize("template", DEPLOYED_TEMPLATES)
def test_no_log_group_name_is_derived_from_a_function(template: str):
    """Rule 3. See the module docstring for why this ordering matters."""
    resources = _resources(template)
    functions = _of_type(resources, *FUNCTION_TYPES)
    derived = []
    for name, group in _of_type(resources, LOG_GROUP_TYPE).items():
        group_name = group.get("Properties", {}).get("LogGroupName")
        for target, _ in _references(group_name):
            if target in functions:
                derived.append(f"{name} -> {target}")
    assert not derived, (
        f"{template}: LogGroupName derived from a function resource, which makes "
        f"the group depend on the function: {derived}"
    )


@pytest.mark.parametrize("template", DEPLOYED_TEMPLATES)
def test_a_log_groups_condition_matches_its_functions(template: str):
    """Rule 4. A mismatch creates one of the pair without the other."""
    resources = _resources(template)
    log_groups = _of_type(resources, LOG_GROUP_TYPE)
    mismatched = []
    for name, function in _of_type(resources, *FUNCTION_TYPES).items():
        destination = _destination(function, log_groups)
        if destination is None:
            continue  # rule 1 reports this
        group = log_groups[destination]
        if function.get("Condition") != group.get("Condition"):
            mismatched.append(
                f"{name} (Condition: {function.get('Condition')}) -> "
                f"{destination} (Condition: {group.get('Condition')})"
            )
    assert not mismatched, (
        f"{template}: condition mismatch between a function and its log group: {mismatched}"
    )


@pytest.mark.parametrize("template", DEPLOYED_TEMPLATES)
def test_every_declared_log_group_is_referenced(template: str):
    """Rule 5. An unreferenced group stays empty while its logs go elsewhere."""
    loaded = _load(template)
    resources = loaded.get("Resources", {}) or {}
    log_groups = _of_type(resources, LOG_GROUP_TYPE)
    referenced = set()
    for name, resource in resources.items():
        if name in log_groups:
            continue  # a group referring to itself does not make it used
        referenced |= {target for target, _ in _references(resource)}
        depends_on = resource.get("DependsOn")
        referenced |= set([depends_on] if isinstance(depends_on, str) else (depends_on or []))
    referenced |= {target for target, _ in _references(loaded.get("Outputs", {}) or {})}
    # A function naming its group by repeating the name counts as using it.
    referenced |= {
        destination
        for function in _of_type(resources, *FUNCTION_TYPES).values()
        if (destination := _destination(function, log_groups)) is not None
    }
    orphans = sorted(set(log_groups) - referenced)
    assert not orphans, (
        f"{template}: log groups nothing points at, so they stay empty while the "
        f"logs they were declared for go to a group this template does not own: {orphans}"
    )
