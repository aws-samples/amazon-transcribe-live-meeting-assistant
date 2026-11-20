# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""The AI stack's template and the nested templates it instantiates, for tests.

`lma-ai-stack.yaml` keeps some of its resources in nested stacks
(`AWS::CloudFormation::Stack` with a local `TemplateURL`), because one template
may hold at most 500 resources after the SAM transform. Most static tests are
about the deployed AI stack as a whole, not about which file a resource is
written in, so `flattened()` returns the parent with every nested template's
resources inlined, as the parent sees them:

* a nested template's `!Ref <Parameter>` becomes the value the parent passes for
  it, and `${Parameter}` in a `!Sub` the variable that value names;
* the parent's `!GetAtt <Stack>.Outputs.<Name>` becomes that output's value;
* each inlined resource inherits the nested stack resource's `DependsOn`, which
  is when CloudFormation creates it;
* the nested stack resource itself is dropped, having been replaced by its
  contents.

So `!Ref ConfigurationTable` in a nested template, passed `!Ref
ConfigurationTable` by the parent, reads exactly as it would have in the parent.
A nested template that cannot be inlined faithfully (a logical-id clash, a
condition declared differently, a parameter passed as something a `!Sub` cannot
name) raises rather than producing a view that hides it.

`test_ai_stack_nested_templates.py` checks the nested templates as files in
their own right: the parameter contract, the outputs the parent reads, and the
names that must differ from the parent's.
"""

from __future__ import annotations

import copy
import re
from pathlib import Path

import yaml

DEPLOYMENT = Path(__file__).resolve().parents[1] / "deployment"
TEMPLATE = DEPLOYMENT / "lma-ai-stack.yaml"

NESTED_STACK_TYPE = "AWS::CloudFormation::Stack"


class CfnLoader(yaml.SafeLoader):
    """YAML loader that tolerates CloudFormation short-form intrinsics."""


def _intrinsic(loader: yaml.Loader, tag_suffix: str, node: yaml.Node):  # noqa: ANN202
    name = f"Fn::{tag_suffix}" if tag_suffix != "Ref" else "Ref"
    if isinstance(node, yaml.ScalarNode):
        return {name: loader.construct_scalar(node)}
    if isinstance(node, yaml.SequenceNode):
        return {name: loader.construct_sequence(node, deep=True)}
    return {name: loader.construct_mapping(node, deep=True)}


CfnLoader.add_multi_constructor("!", _intrinsic)


def load(path: Path) -> dict:
    return yaml.load(path.read_text(encoding="utf-8"), Loader=CfnLoader)


def nested_templates(template: dict | None = None) -> dict[str, Path]:
    """Logical id of each nested stack resource with a local template -> its path."""
    template = template if template is not None else load(TEMPLATE)
    found = {}
    for name, resource in (template.get("Resources") or {}).items():
        if resource.get("Type") != NESTED_STACK_TYPE:
            continue
        url = (resource.get("Properties") or {}).get("TemplateURL")
        if isinstance(url, str) and not url.startswith(("https://", "s3://")):
            found[name] = (DEPLOYMENT / url).resolve()
    return found


def all_template_paths() -> list[Path]:
    """The AI stack's template followed by every nested template it instantiates."""
    return [TEMPLATE, *nested_templates().values()]


def _getatt_target(value) -> tuple[str, str] | None:
    if isinstance(value, list) and len(value) == 2:
        return str(value[0]), str(value[1])
    if isinstance(value, str) and "." in value:
        resource, attribute = value.split(".", 1)
        return resource, attribute
    return None


def _sub_name(value) -> str:
    """The `${...}` variable that names `value` inside a `!Sub`."""
    if isinstance(value, dict) and set(value) == {"Ref"}:
        return value["Ref"]
    if isinstance(value, dict) and set(value) == {"Fn::GetAtt"}:
        target = _getatt_target(value["Fn::GetAtt"])
        if target:
            return f"{target[0]}.{target[1]}"
    raise ValueError(f"cannot name {value!r} inside a !Sub")


def _substitute(node, values: dict, reserved: frozenset = frozenset()):
    """Replace `Ref`s to the names in `values` (and `${name}` in a `Sub`)."""
    if isinstance(node, dict):
        if set(node) == {"Ref"} and node["Ref"] in values and node["Ref"] not in reserved:
            return copy.deepcopy(values[node["Ref"]])
        if set(node) == {"Fn::Sub"}:
            body = node["Fn::Sub"]
            variables = {}
            if isinstance(body, list):
                body, variables = body[0], body[1]
            local = frozenset(variables) | reserved

            def rename(match: re.Match) -> str:
                name = match.group(1)
                if name in values and name not in local:
                    return "${" + _sub_name(values[name]) + "}"
                return match.group(0)

            body = re.sub(r"\$\{([^}!]+)\}", rename, body)
            if variables:
                return {"Fn::Sub": [body, _substitute(variables, values, reserved)]}
            return {"Fn::Sub": body}
        return {key: _substitute(value, values, reserved) for key, value in node.items()}
    if isinstance(node, list):
        return [_substitute(value, values, reserved) for value in node]
    return node


def _replace_outputs(node, outputs: dict[str, dict]):
    """Replace `!GetAtt <Stack>.Outputs.<Name>` with that output's value."""
    if isinstance(node, dict):
        if set(node) == {"Fn::GetAtt"}:
            target = _getatt_target(node["Fn::GetAtt"])
            if target and target[0] in outputs and target[1].startswith("Outputs."):
                name = target[1][len("Outputs.") :]
                return copy.deepcopy(outputs[target[0]][name])
        if set(node) == {"Fn::Sub"}:
            body = node["Fn::Sub"]
            variables = {}
            if isinstance(body, list):
                body, variables = body[0], body[1]

            def rename(match: re.Match) -> str:
                stack, _, name = match.group(1).partition(".Outputs.")
                if name and stack in outputs:
                    return "${" + _sub_name(outputs[stack][name]) + "}"
                return match.group(0)

            body = re.sub(r"\$\{([^}!]+)\}", rename, body)
            if variables:
                return {"Fn::Sub": [body, _replace_outputs(variables, outputs)]}
            return {"Fn::Sub": body}
        return {key: _replace_outputs(value, outputs) for key, value in node.items()}
    if isinstance(node, list):
        return [_replace_outputs(value, outputs) for value in node]
    return node


def _as_list(value) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def flattened() -> dict:
    """The AI stack's template with every nested template's resources inlined."""
    parent = load(TEMPLATE)
    resources = dict(parent.get("Resources") or {})
    conditions = dict(parent.get("Conditions") or {})
    outputs_by_stack: dict[str, dict] = {}
    inlined: dict[str, dict] = {}

    for stack, path in nested_templates(parent).items():
        stack_resource = resources.pop(stack)
        if "Condition" in stack_resource:
            raise ValueError(f"{stack}: inlining a conditional nested stack is not supported")
        child = load(path)
        if (child.get("Globals") or {}) != (parent.get("Globals") or {}):
            raise ValueError(f"{path.name} declares different SAM Globals from the AI stack")
        passed = (stack_resource.get("Properties") or {}).get("Parameters") or {}
        values = {
            name: passed[name] if name in passed else {"Ref": name}
            for name in (child.get("Parameters") or {})
        }
        for name, condition in (child.get("Conditions") or {}).items():
            condition = _substitute(condition, values)
            if name in conditions and conditions[name] != condition:
                raise ValueError(f"{path.name}: condition {name} differs from the AI stack's")
            conditions[name] = condition
        inherited = _as_list(stack_resource.get("DependsOn"))
        for name, resource in (child.get("Resources") or {}).items():
            if name in resources or name in inlined:
                raise ValueError(f"{path.name}: logical id {name} is also declared elsewhere")
            resource = _substitute(resource, values)
            if inherited:
                resource["DependsOn"] = list(
                    dict.fromkeys(_as_list(resource.get("DependsOn")) + inherited)
                )
            inlined[name] = resource
        outputs_by_stack[stack] = {
            name: _substitute(output["Value"], values)
            for name, output in (child.get("Outputs") or {}).items()
        }

    resources.update(inlined)
    flat = dict(parent)
    flat["Resources"] = _replace_outputs(resources, outputs_by_stack)
    flat["Outputs"] = _replace_outputs(parent.get("Outputs") or {}, outputs_by_stack)
    flat["Conditions"] = conditions
    return flat
