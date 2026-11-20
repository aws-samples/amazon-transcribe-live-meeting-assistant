# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""The AI stack's nested templates, checked as files in their own right (no AWS).

The other AI stack tests read the stack with its nested templates inlined
(`ai_stack_templates.flattened`), which is right for what the deployed stack
does but hides the seams: a parameter the parent does not pass, an output it
reads that is not declared, a name that collides with one the parent still
holds. All of those fail a deploy, the last of them only on the update that
moves a resource, because CloudFormation creates the moved copy before it
deletes the original. They are asserted here.

Run via `make test-ai-stack` or pytest directly.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from ai_stack_templates import DEPLOYMENT, TEMPLATE, flattened, load, nested_templates

REPO_ROOT = Path(__file__).resolve().parents[2]

PSEUDO_PARAMETERS = {
    "AWS::AccountId",
    "AWS::NoValue",
    "AWS::NotificationARNs",
    "AWS::Partition",
    "AWS::Region",
    "AWS::StackId",
    "AWS::StackName",
    "AWS::URLSuffix",
}

#: Kept in the AI stack itself: moving one would replace it, and with it the data
#: or identity it holds.
STATEFUL_TYPES = {
    "AWS::AppSync::GraphQLApi",
    "AWS::AppSync::GraphQLSchema",
    "AWS::Cognito::UserPool",
    "AWS::Cognito::UserPoolClient",
    "AWS::DynamoDB::Table",
    "AWS::KMS::Key",
    "AWS::Kinesis::Stream",
    "AWS::S3::Bucket",
    "AWS::SQS::Queue",
    "AWS::SecretsManager::Secret",
}

PARENT = load(TEMPLATE)
NESTED = nested_templates(PARENT)
NESTED_IDS = sorted(NESTED)


def _child(stack: str) -> dict:
    return load(NESTED[stack])


def _passed(stack: str) -> dict:
    return PARENT["Resources"][stack]["Properties"].get("Parameters") or {}


def _walk(node, path: str = ""):
    """Yield (kind, target, path) for every Ref, GetAtt and Sub variable."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "Ref" and isinstance(value, str):
                yield ("Ref", value, path)
            elif key == "Fn::GetAtt":
                target = value if isinstance(value, list) else str(value).split(".", 1)
                yield ("GetAtt", tuple(target), path)
            elif key == "Fn::Sub":
                body, variables = (value[0], value[1]) if isinstance(value, list) else (value, {})
                for name in re.findall(r"\$\{([^}!]+)\}", body):
                    if name not in variables:
                        yield ("Sub", name, path)
                yield from _walk(variables, f"{path}/Fn::Sub")
                continue
            elif key == "Fn::If" and isinstance(value, list) and value:
                yield ("Condition", value[0], path)
            yield from _walk(value, f"{path}/{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _walk(value, f"{path}[{index}]")


def test_the_ai_stack_has_a_nested_template() -> None:
    """Guards the parametrised tests below against iterating over nothing."""
    assert "LambdaResolversStack" in NESTED


@pytest.mark.parametrize("stack", NESTED_IDS)
def test_the_template_url_resolves_from_both_places_it_is_read(stack: str) -> None:
    """`sam build` reads the AI stack from lma-ai-stack/out/ (the substituted copy
    `make release` writes there), cfn-lint and these tests from deployment/. A
    path through ../deployment/ is the same file from both."""
    url = PARENT["Resources"][stack]["Properties"]["TemplateURL"]
    assert url.startswith("../deployment/"), url
    assert (DEPLOYMENT.parent / "out" / url).resolve() == (DEPLOYMENT / url).resolve()
    assert NESTED[stack].is_file()


@pytest.mark.parametrize("stack", NESTED_IDS)
def test_the_parent_passes_exactly_the_declared_parameters(stack: str) -> None:
    declared = _child(stack).get("Parameters") or {}
    passed = _passed(stack)
    required = {name for name, spec in declared.items() if "Default" not in spec}
    assert not required - set(passed), f"not passed: {sorted(required - set(passed))}"
    assert not set(passed) - set(declared), f"not declared: {sorted(set(passed) - set(declared))}"


@pytest.mark.parametrize("stack", NESTED_IDS)
def test_the_stack_name_it_is_given_is_the_ai_stacks(stack: str) -> None:
    """Its log groups and policy names are built from this, as they were in the
    AI stack, so the troubleshooting agent and `lma logs` find them under the
    AI stack's prefix."""
    assert _passed(stack).get("AIStackName") == {"Ref": "AWS::StackName"}


@pytest.mark.parametrize("stack", NESTED_IDS)
def test_every_reference_resolves_within_the_template(stack: str) -> None:
    child = _child(stack)
    resources = child.get("Resources") or {}
    names = set(resources) | set(child.get("Parameters") or {}) | PSEUDO_PARAMETERS
    conditions = set(child.get("Conditions") or {})
    unresolved = []
    for kind, target, path in _walk({"Resources": resources, "Outputs": child.get("Outputs")}):
        if kind in ("Ref", "Sub"):
            name = (
                target.split(".")[0] if kind == "Sub" and not target.startswith("AWS::") else target
            )
            if name not in names:
                unresolved.append(f"{kind} {target} at {path}")
        elif kind == "GetAtt" and target[0] not in resources:
            unresolved.append(f"GetAtt {'.'.join(target)} at {path}")
        elif kind == "Condition" and target not in conditions:
            unresolved.append(f"Condition {target} at {path}")
    unresolved += [
        f"Condition {r['Condition']} on {name}"
        for name, r in resources.items()
        if r.get("Condition") and r["Condition"] not in conditions
    ]
    assert not unresolved, unresolved


@pytest.mark.parametrize("stack", NESTED_IDS)
def test_every_parameter_is_used(stack: str) -> None:
    child = _child(stack)
    used = {
        target.split(".")[0] if kind == "Sub" else target
        for kind, target, _ in _walk(child)
        if kind in ("Ref", "Sub")
    }
    assert not set(child.get("Parameters") or {}) - used


def test_every_output_the_ai_stack_reads_is_declared() -> None:
    missing = []
    for kind, target, path in _walk(PARENT):
        if kind == "GetAtt" and target[0] in NESTED:
            output = target[1].removeprefix("Outputs.")
            if output not in (_child(target[0]).get("Outputs") or {}):
                missing.append(f"{target[0]}.{target[1]} at {path}")
        if kind == "Sub" and target.split(".")[0] in NESTED:
            stack, _, output = target.partition(".Outputs.")
            if output not in (_child(stack).get("Outputs") or {}):
                missing.append(f"{target} at {path}")
    assert not missing, missing


@pytest.mark.parametrize("stack", NESTED_IDS)
def test_nothing_stateful_is_in_a_nested_template(stack: str) -> None:
    found = {
        name: resource["Type"]
        for name, resource in (_child(stack).get("Resources") or {}).items()
        if resource.get("Type") in STATEFUL_TYPES
    }
    assert not found, f"keep these in lma-ai-stack.yaml: {found}"


@pytest.mark.parametrize("stack", NESTED_IDS)
def test_functions_take_generated_names(stack: str) -> None:
    """A generated name cannot collide with the function's name while it was in
    the AI stack, and cannot outgrow Lambda's 64 characters under a nested
    stack's longer name."""
    named = [
        name
        for name, resource in (_child(stack).get("Resources") or {}).items()
        if resource.get("Type") in ("AWS::Serverless::Function", "AWS::Lambda::Function")
        and "FunctionName" in (resource.get("Properties") or {})
    ]
    assert not named, named


def _log_group_names(template: dict) -> dict[str, str]:
    names = {}
    for name, resource in (template.get("Resources") or {}).items():
        if resource.get("Type") != "AWS::Logs::LogGroup":
            continue
        value = (resource.get("Properties") or {}).get("LogGroupName")
        if isinstance(value, dict) and "Fn::Sub" in value and isinstance(value["Fn::Sub"], str):
            value = value["Fn::Sub"]
        names[name] = value
    return names


@pytest.mark.parametrize("stack", NESTED_IDS)
def test_log_groups_sit_under_the_ai_stack_prefix_with_names_of_their_own(stack: str) -> None:
    """Under `/<AI stack name>/`, which is the prefix the troubleshooting agent's
    log discovery and its read grant cover, and the agent sorts a group into a
    component by the `/lambda/<function>` it ends with. But never
    `/<AI stack name>/lambda/...`, the AI stack's own scheme: a group moved here
    keeping that name would collide with the original during the update."""
    parent_names = {
        str(value).replace("${AWS::StackName}", "${AIStackName}")
        for value in _log_group_names(PARENT).values()
    }
    wrong = []
    for name, value in _log_group_names(_child(stack)).items():
        if not isinstance(value, str) or not value.startswith("/${AIStackName}/"):
            wrong.append(f"{name}: {value!r} is not under /${{AIStackName}}/")
        elif value.startswith("/${AIStackName}/lambda/") or value in parent_names:
            wrong.append(f"{name}: {value!r} uses the AI stack's own naming")
        elif not re.search(r"/lambda/\w+$", value):
            wrong.append(f"{name}: {value!r} does not end with /lambda/<function>")
    assert not wrong, wrong


def _data_source_names(template: dict) -> list:
    return [
        (resource.get("Properties") or {}).get("Name")
        for resource in (template.get("Resources") or {}).values()
        if resource.get("Type") == "AWS::AppSync::DataSource"
    ]


def test_appsync_data_source_names_are_unique_across_the_stack() -> None:
    """AppSync names are unique per API. A data source moved into a nested
    template also needs a name other than the one it had in the AI stack, for
    the reason the log group test gives; that history is not in the tree, so the
    move itself has to see to it."""
    names = _data_source_names(flattened())
    duplicates = sorted({name for name in names if names.count(name) > 1})
    assert not duplicates, duplicates


def test_every_nested_template_is_linted_and_log_checked() -> None:
    """`CFN_TEMPLATES` is what `make lint-cfn` lints and what
    test_log_destinations.py checks."""
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    for path in NESTED.values():
        relative = path.relative_to(REPO_ROOT / "lma-ai-stack")
        assert f"$(AI_STACK_DIR)/{relative}" in makefile, f"{relative} is not in CFN_TEMPLATES"


@pytest.mark.parametrize("stack", NESTED_IDS)
def test_the_sam_globals_match_the_ai_stacks(stack: str) -> None:
    """A function moved here keeps the environment the AI stack's Globals gave it."""
    assert _child(stack).get("Globals") == PARENT.get("Globals")
