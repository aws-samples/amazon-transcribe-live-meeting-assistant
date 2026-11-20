# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""The AI stack's templates stay under CloudFormation's resource limit (no AWS).

CloudFormation refuses a template that declares more than 500 resources, and it
counts them after the SAM transform, which expands each `AWS::Serverless::*`
resource into several (a function without a declared role gets a generated
one, a layer becomes a `AWS::Lambda::LayerVersion`, a scheduled event becomes a
rule and a permission). The template as written is therefore always smaller
than the one CloudFormation counts. The refusal arrives only when the stack
update is submitted -- "Template format error: Number of resources, N, is
greater than maximum allowed, 500" -- and the update then rolls back, so it is
counted here instead, with the transform CloudFormation itself runs.

The budget is set below the limit so that two features landing in the same
release cannot together take the stack over it. When a change fails this test,
make room rather than raising the budget: move a cohesive group of stateless
resources into a nested template. `lma-ai-lambda-resolvers.yaml` is where a
Lambda function behind an AppSync data source goes (see its header for the
rules); anything else can go into a new nested stack declared the same way.
Never move a table, bucket, key, user pool, stream or the API itself, and give
every moved resource that has a fixed name -- a log group, an AppSync data
source, a function with a `FunctionName` -- a name that differs from the one it
had, because CloudFormation creates the new copy before it deletes the old one.
`test_ai_stack_nested_templates.py` checks those names.

Run via `make test-ai-stack` or pytest directly.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from ai_stack_templates import TEMPLATE, all_template_paths

CLOUDFORMATION_LIMIT = 500

#: The most resources any one AI stack template may hold after the SAM transform.
BUDGET = 470

try:
    from samtranslator.translator.transform import transform as sam_transform
except ImportError:  # pragma: no cover - reported by the fixture below
    sam_transform = None


class _CfnJsonLoader(yaml.SafeLoader):
    """Reads short-form intrinsics into the JSON form the SAM translator expects.

    Differs from the other loaders in these tests in one way that matters to the
    translator: `!GetAtt A.B` becomes the list `["A", "B"]`, as in JSON.
    """


def _intrinsic(loader: yaml.Loader, tag_suffix: str, node: yaml.Node):  # noqa: ANN202
    if tag_suffix in ("Ref", "Condition"):
        name = tag_suffix
    else:
        name = f"Fn::{tag_suffix}"
    if isinstance(node, yaml.ScalarNode):
        value = loader.construct_scalar(node)
        if name == "Fn::GetAtt":
            value = value.split(".", 1)
        return {name: value}
    if isinstance(node, yaml.SequenceNode):
        return {name: loader.construct_sequence(node, deep=True)}
    return {name: loader.construct_mapping(node, deep=True)}


_CfnJsonLoader.add_multi_constructor("!", _intrinsic)


# Local artifact paths, which `sam build` and `sam package` replace with S3
# locations. The translator validates that they are S3 locations, and their
# contents do not change how many resources a template becomes.
_ARTIFACT_PROPERTIES = {
    "AWS::Serverless::Function": ("CodeUri", "s3://artifacts/function.zip"),
    "AWS::Serverless::LayerVersion": ("ContentUri", "s3://artifacts/layer.zip"),
    "AWS::Serverless::StateMachine": ("DefinitionUri", "s3://artifacts/definition.json"),
    "AWS::Serverless::Api": ("DefinitionUri", "s3://artifacts/openapi.yaml"),
    "AWS::Serverless::HttpApi": ("DefinitionUri", "s3://artifacts/openapi.yaml"),
}


class _NoManagedPolicies:
    """The translator asks for this only to resolve a managed policy given by name."""

    def load(self) -> dict:
        return {}


def _transformed_resources(path: Path) -> dict:
    # Through JSON so that an unquoted `Version: 2012-10-17`, which YAML reads as a
    # date, reaches the translator as the string CloudFormation reads it as.
    template = json.loads(
        json.dumps(yaml.load(path.read_text(encoding="utf-8"), Loader=_CfnJsonLoader), default=str)
    )
    if "Transform" not in template:
        return template.get("Resources") or {}
    for resource in (template.get("Resources") or {}).values():
        artifact = _ARTIFACT_PROPERTIES.get(resource.get("Type"))
        properties = resource.get("Properties") or {}
        if artifact and isinstance(properties.get(artifact[0]), str):
            properties[artifact[0]] = artifact[1]
    globals_function = (template.get("Globals") or {}).get("Function") or {}
    if isinstance(globals_function.get("CodeUri"), str):
        globals_function["CodeUri"] = _ARTIFACT_PROPERTIES["AWS::Serverless::Function"][1]
    return sam_transform(template, {}, _NoManagedPolicies())["Resources"]


@pytest.fixture(scope="module")
def counts() -> dict[str, int]:
    if sam_transform is None:
        pytest.fail(
            "aws-sam-translator is not installed. `make setup-python` installs it; "
            "otherwise `pip install aws-sam-translator`."
        )
    return {path.name: len(_transformed_resources(path)) for path in all_template_paths()}


def test_the_budget_leaves_room_below_the_cloudformation_limit() -> None:
    assert BUDGET <= CLOUDFORMATION_LIMIT - 30


def test_the_count_is_taken_after_the_transform(counts: dict[str, int]) -> None:
    """Guards the measurement: SAM resources expand, so the transformed count of
    the AI stack exceeds what it declares, and no SAM type survives the transform.
    A count that matched the declared one would mean the transform did not run."""
    declared = yaml.load(TEMPLATE.read_text(encoding="utf-8"), Loader=_CfnJsonLoader)["Resources"]
    transformed = _transformed_resources(TEMPLATE)
    assert counts[TEMPLATE.name] == len(transformed) > len(declared)
    assert not [
        name for name, r in transformed.items() if r["Type"].startswith("AWS::Serverless::")
    ]


@pytest.mark.parametrize("name", [path.name for path in all_template_paths()])
def test_each_ai_stack_template_stays_within_the_resource_budget(
    counts: dict[str, int], name: str
) -> None:
    count = counts[name]
    assert count <= BUDGET, (
        f"{name} holds {count} resources after the SAM transform. The budget is "
        f"{BUDGET}, and CloudFormation refuses a template with more than "
        f"{CLOUDFORMATION_LIMIT}. Do not raise the budget: move a cohesive group of "
        f"stateless resources into a nested template, as the docstring of "
        f"lma-ai-stack/test/test_resource_budget.py describes."
    )
