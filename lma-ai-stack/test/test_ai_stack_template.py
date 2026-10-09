# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Static tests for the AI stack's CloudFormation template (no AWS).

`lma-ai-stack/deployment/lma-ai-stack.yaml` is 8,400 lines and 287 resources, and
it is the stack that takes longest to deploy and roll back. The equivalent tests
for the Virtual Participant template have repeatedly caught deploy-blocking
mistakes in seconds; this is the same idea applied to the larger template.

Two kinds of assertion, kept visibly apart:

* **Invariants that hold today**, asserted plainly. A mistyped `!Ref`, a resolver
  pointing at a data source that does not exist, or an output `lma-main.yaml`
  consumes being renamed all fail a deploy — 35-40 minutes in, followed by a
  rollback — and all of them are cheap to catch here instead.
* **Known debt**, asserted against an explicit allowlist of the resources that do
  not yet comply. These are not aspirational: each names exactly what is exempt,
  so a *newly* non-compliant resource fails immediately while the existing ones
  stay visible as a list to work through. Fixing one means deleting a line here,
  which is the point.

Run via `make test-ai-template` or pytest directly.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

TEMPLATE = Path(__file__).resolve().parents[1] / "deployment" / "lma-ai-stack.yaml"
MAIN_TEMPLATE = Path(__file__).resolve().parents[2] / "lma-main.yaml"

# Substituted by CloudFormation rather than declared in the template.
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

STATEFUL_TYPES = {
    "AWS::DynamoDB::Table",
    "AWS::S3::Bucket",
    "AWS::SQS::Queue",
    "AWS::SecretsManager::Secret",
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


@pytest.fixture(scope="module")
def template() -> dict:
    return yaml.load(TEMPLATE.read_text(), Loader=_CfnLoader)


@pytest.fixture(scope="module")
def raw() -> str:
    return TEMPLATE.read_text()


@pytest.fixture(scope="module")
def resources(template: dict) -> dict:
    return template["Resources"]


def _of_type(resources: dict, *types: str) -> dict:
    return {k: v for k, v in resources.items() if v.get("Type") in types}


def _functions(resources: dict) -> dict:
    return _of_type(resources, "AWS::Serverless::Function", "AWS::Lambda::Function")


# ── the parser itself ───────────────────────────────────────────────────────


def test_the_template_parses_at_the_expected_scale(resources: dict):
    """Guards every other test here against collecting nothing.

    They all iterate over what the loader produced, so a template that was split
    in two, renamed, or parsed into an empty mapping would otherwise leave this
    file green having checked nothing. It does not detect a loader that still
    finds every resource but stops resolving intrinsics — the reference tests
    catch that, because an unresolved `!Ref` stops looking like a Ref at all.
    """
    assert len(resources) >= 250, f"only parsed {len(resources)} resources"
    assert len(_functions(resources)) >= 30
    assert len(_of_type(resources, "AWS::AppSync::Resolver")) >= 70


# ── references that must resolve, or the deploy fails ───────────────────────


def _walk(node, path: str = ""):
    """Yield (kind, target, path) for every Ref / GetAtt / Condition reference."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "Ref" and isinstance(value, str):
                yield ("Ref", value, path)
            elif key == "Fn::GetAtt":
                target = value[0] if isinstance(value, list) else str(value).split(".")[0]
                if isinstance(target, str):
                    yield ("GetAtt", target, path)
            elif key == "Fn::If" and isinstance(value, list) and value:
                if isinstance(value[0], str):
                    yield ("Condition", value[0], path)
            yield from _walk(value, f"{path}/{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _walk(value, f"{path}[{index}]")


def test_every_ref_resolves_to_something_declared(template: dict, resources: dict):
    """A mistyped `!Ref` is accepted by cfn-lint's default rules and fails at deploy."""
    known = set(resources) | set(template.get("Parameters", {})) | PSEUDO_PARAMETERS
    unresolved = [
        f"{target} (at {path})"
        for kind, target, path in _walk(
            {"Resources": resources, "Outputs": template.get("Outputs", {})}
        )
        if kind == "Ref" and target not in known
    ]
    assert not unresolved, f"unresolvable Ref targets: {unresolved}"


def test_every_getatt_resolves_to_a_declared_resource(template: dict, resources: dict):
    known = set(resources) | PSEUDO_PARAMETERS
    unresolved = [
        f"{target} (at {path})"
        for kind, target, path in _walk(
            {"Resources": resources, "Outputs": template.get("Outputs", {})}
        )
        if kind == "GetAtt" and target not in known
    ]
    assert not unresolved, f"unresolvable GetAtt targets: {unresolved}"


def test_every_condition_named_anywhere_is_declared(template: dict, resources: dict):
    """Covers both `Fn::If` and a resource's own top-level `Condition:` key."""
    declared = set(template.get("Conditions", {}))
    unknown = {
        target
        for kind, target, _ in _walk(
            {"Resources": resources, "Outputs": template.get("Outputs", {})}
        )
        if kind == "Condition" and target not in declared
    }
    unknown |= {
        value["Condition"]
        for value in resources.values()
        if isinstance(value.get("Condition"), str) and value["Condition"] not in declared
    }
    assert not unknown, f"conditions used but not declared: {sorted(unknown)}"


def test_every_appsync_resolver_points_at_a_declared_data_source(resources: dict):
    """A resolver attached to a missing data source fails when the stack deploys."""
    data_sources = set(_of_type(resources, "AWS::AppSync::DataSource"))
    broken = []
    for name, resource in _of_type(resources, "AWS::AppSync::Resolver").items():
        reference = resource.get("Properties", {}).get("DataSourceName")
        if not isinstance(reference, dict):
            continue
        target = reference.get("Fn::GetAtt")
        if target is None:
            continue
        target = target[0] if isinstance(target, list) else str(target).split(".")[0]
        if target not in data_sources:
            broken.append(f"{name} -> {target}")
    assert not broken, f"resolvers naming a data source that does not exist: {broken}"


def test_every_output_the_main_template_consumes_is_declared(template: dict):
    """The cross-template contract: renaming an output here breaks lma-main.yaml.

    lma-main.yaml reads these as `!GetAtt AISTACK.Outputs.<Name>`, which fails at
    deploy time in the parent stack — after the AI stack itself has succeeded.
    """
    main = MAIN_TEMPLATE.read_text()
    consumed = set(re.findall(r"AISTACK\s*\.\s*Outputs\s*\.\s*(\w+)", main))
    consumed |= set(re.findall(r'"AISTACK"\s*,\s*"Outputs\.(\w+)"', main))
    assert len(consumed) >= 25, f"only found {len(consumed)} consumed outputs — regex stale?"
    missing = sorted(consumed - set(template.get("Outputs", {})))
    assert not missing, f"lma-main.yaml consumes outputs this template does not declare: {missing}"


def test_every_function_code_uri_exists_on_disk(resources: dict):
    """`sam build` resolves these relative to the template; a stale path fails the build."""
    missing = []
    for name, resource in _of_type(resources, "AWS::Serverless::Function").items():
        code_uri = resource.get("Properties", {}).get("CodeUri")
        if not isinstance(code_uri, str):
            continue
        if not (TEMPLATE.parent / code_uri).resolve().exists():
            missing.append(f"{name} -> {code_uri}")
    assert not missing, f"CodeUri paths that do not exist: {missing}"


# ── conventions that hold today ─────────────────────────────────────────────


def test_every_log_group_is_encrypted_with_the_customer_managed_key(resources: dict):
    """All 34 comply, so this is a guard against the next one added without it."""
    unencrypted = [
        name
        for name, resource in _of_type(resources, "AWS::Logs::LogGroup").items()
        if "KmsKeyId" not in resource.get("Properties", {})
    ]
    assert not unencrypted, f"log groups without KmsKeyId: {unencrypted}"


def test_every_cfn_nag_suppression_gives_a_reason(resources: dict):
    """A suppression without a rationale cannot be reviewed, only trusted."""
    unexplained = []
    for name, resource in resources.items():
        metadata = resource.get("Metadata") or {}
        rules = (metadata.get("cfn_nag") or {}).get("rules_to_suppress") or []
        for rule in rules:
            if not (rule.get("reason") or "").strip():
                unexplained.append(f"{name}:{rule.get('id')}")
    assert not unexplained, f"cfn_nag suppressions with no reason: {unexplained}"


def test_every_lambda_runtime_is_the_same_pinned_python(resources: dict):
    """A stray runtime is how one function ends up on an unsupported version."""
    runtimes = {
        str(resource.get("Properties", {}).get("Runtime"))
        for resource in _functions(resources).values()
        if resource.get("Properties", {}).get("Runtime")
    }
    assert runtimes == {"python3.12"}, f"expected only python3.12, found {sorted(runtimes)}"


# ── known debt, named ───────────────────────────────────────────────────────
#
# Each allowlist is the current exemption set, not a target. A new resource that
# does not comply fails the corresponding test; fixing an existing one means
# removing its name from the list below.

# These two are custom-resource helpers that predate the convention.
FUNCTIONS_WITHOUT_A_DEDICATED_LOG_GROUP = {
    "GetCloudFrontPrefixListFunction",
    "GetEventApiDnsFunction",
}

FUNCTIONS_NOT_DEPENDING_ON_THEIR_LOG_GROUP = {
    "MicrovmVncTokenFunction",
}

STATEFUL_RESOURCES_WITHOUT_A_DELETION_POLICY = {
    "VncEdgeTokenSigningSecret",
    "VncOriginVerifyHeaderValueSecret",
    "WebAppBucket",
}


def test_functions_declare_a_dedicated_log_group(resources: dict):
    """Without one, Lambda creates its own with a retention and key we do not set."""
    without = {
        name
        for name, resource in _functions(resources).items()
        if not (resource.get("Properties", {}).get("LoggingConfig") or {}).get("LogGroup")
    }
    assert without == FUNCTIONS_WITHOUT_A_DEDICATED_LOG_GROUP, (
        f"newly missing a LoggingConfig.LogGroup: "
        f"{sorted(without - FUNCTIONS_WITHOUT_A_DEDICATED_LOG_GROUP)}; "
        f"now compliant (remove from the allowlist): "
        f"{sorted(FUNCTIONS_WITHOUT_A_DEDICATED_LOG_GROUP - without)}"
    )


def test_functions_depend_on_the_log_group_they_name(resources: dict):
    """Without the DependsOn, Lambda can win the race and create a conflicting group."""
    without = set()
    for name, resource in _functions(resources).items():
        logging_config = resource.get("Properties", {}).get("LoggingConfig") or {}
        log_group = logging_config.get("LogGroup")
        if not isinstance(log_group, dict) or "Ref" not in log_group:
            continue
        declared = resource.get("DependsOn")
        declared = [declared] if isinstance(declared, str) else (declared or [])
        if log_group["Ref"] not in declared:
            without.add(name)
    assert without == FUNCTIONS_NOT_DEPENDING_ON_THEIR_LOG_GROUP, (
        f"newly missing the DependsOn: "
        f"{sorted(without - FUNCTIONS_NOT_DEPENDING_ON_THEIR_LOG_GROUP)}; "
        f"now compliant: {sorted(FUNCTIONS_NOT_DEPENDING_ON_THEIR_LOG_GROUP - without)}"
    )


def test_stateful_resources_set_a_deletion_policy(resources: dict):
    """Otherwise the default applies and the data goes with the stack."""
    without = {
        name
        for name, resource in _of_type(resources, *STATEFUL_TYPES).items()
        if "DeletionPolicy" not in resource
    }
    assert without == STATEFUL_RESOURCES_WITHOUT_A_DELETION_POLICY, (
        f"newly missing a DeletionPolicy: "
        f"{sorted(without - STATEFUL_RESOURCES_WITHOUT_A_DELETION_POLICY)}; "
        f"now compliant: {sorted(STATEFUL_RESOURCES_WITHOUT_A_DELETION_POLICY - without)}"
    )


def test_iam_roles_carry_the_permissions_boundary_conditional(resources: dict):
    """`PermissionsBoundaryArn` only takes effect on roles that reference it.

    The parameter is documented as not yet usable with a non-empty value for
    exactly this reason, so the count is the measure of that gap. Asserted as an
    exact number rather than a ceiling so that the figure in the documentation and
    the figure in the template cannot drift apart silently.
    """
    roles = _of_type(resources, "AWS::IAM::Role")
    without = {
        name
        for name, resource in roles.items()
        if "PermissionsBoundary" not in resource.get("Properties", {})
    }
    assert len(without) == 35, (
        f"{len(without)} of {len(roles)} IAM roles do not reference "
        f"PermissionsBoundaryArn, expected 35. If a role was added, give it the "
        f"`!If [HasPermissionsBoundary, ...]` block; if one was fixed, lower this "
        f"number and update docs/cloudformation-service-role.md. Roles: "
        f"{sorted(without)}"
    )


def test_partition_and_endpoint_literals_do_not_increase(raw: str):
    """`arn:aws:` and `amazonaws.com` literals break GovCloud and China regions.

    A ratchet rather than a clean assertion: the template has a long tail of these
    predating the rule. Counted by line so a new one cannot be added quietly, and
    so the numbers have to come down deliberately.
    """
    arn_literals = 0
    endpoint_literals = 0
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or "AllowedPattern" in line:
            continue
        if "arn:aws:" in line:
            arn_literals += 1
        if "amazonaws.com" in line and "URLSuffix" not in line:
            endpoint_literals += 1
    assert arn_literals <= 92, (
        f"{arn_literals} lines hardcode 'arn:aws:' (was 92). Use "
        f'!Sub "arn:${{AWS::Partition}}:..." instead.'
    )
    assert endpoint_literals <= 89, (
        f"{endpoint_literals} lines hardcode 'amazonaws.com' (was 89). Use "
        f'!Sub "service.${{AWS::URLSuffix}}" instead.'
    )
