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


@pytest.fixture(scope="module")
def main_template() -> dict:
    return yaml.load(MAIN_TEMPLATE.read_text(), Loader=_CfnLoader)


def test_both_prompt_template_stacks_are_given_the_same_domain(main_template: dict):
    """One domain, or the two tables hold different prompts.

    The llm-template-setup stack still seeds the table a rollback reads, and the
    AI stack seeds the shared configuration table. Each is told which set of
    summary prompts this deployment ships. If the two values drifted, one table
    would hold the general prompts and the other the healthcare ones, and which
    a deployment used would depend on whether it had been rolled back.
    """
    resources = main_template["Resources"]
    domains = {
        name: resources[name]["Properties"]["Parameters"]["Domain"]
        for name in ("AISTACK", "LLMTEMPLATESTACK")
    }
    assert len(set(domains.values())) == 1, f"the two stacks are given different domains: {domains}"


def test_the_bedrock_model_choices_match_the_main_templates(template: dict, main_template: dict):
    """The other cross-template contract: both templates list the selectable models.

    lma-main.yaml passes its own `BedrockModelId` straight through to this stack
    (`BedrockModelId: !Ref BedrockModelId` on the AISTACK resource), so this
    template's `AllowedValues` has to admit everything lma-main offers. A value
    offered there and missing here fails the nested stack's update on parameter
    validation — in the parent's eyes, 35-40 minutes into a deploy, followed by a
    rollback.

    Asserted as equality rather than containment because the two lists are meant
    to be the same list. They have drifted before: this template kept offering
    Claude 3.x models for several releases after lma-main.yaml stopped, which was
    invisible precisely because the direction of the drift was the harmless one.
    If a model should genuinely be selectable in one template only, this is the
    test to change deliberately.
    """
    here = template["Parameters"]["BedrockModelId"]["AllowedValues"]
    there = main_template["Parameters"]["BedrockModelId"]["AllowedValues"]
    assert len(here) >= 15, f"only {len(here)} model choices — did the list move?"
    assert set(here) == set(there), (
        "BedrockModelId AllowedValues differ between this template and "
        f"lma-main.yaml. Only here: {sorted(set(here) - set(there))}. "
        f"Only in lma-main.yaml: {sorted(set(there) - set(here))}"
    )


def test_the_summary_model_default_matches_across_the_templates(
    template: dict, main_template: dict
):
    """The two `BedrockModelId` defaults are the same parameter and must agree.

    `lma-main.yaml` passes its value straight into the nested AI stack, so the
    nested default is reached only by deploying that template standalone. If the
    two drift, a standalone deployment summarises with a different model from
    the one the documented path uses, and no deploy-time check would catch it.

    The Meeting Assistant's default is deliberately NOT included. It is chosen
    for a different job: summarisation is batch work where answer quality
    dominates, while the assistant answers mid-meeting and is sometimes spoken
    aloud, so its time to first token is what matters. The two are free to
    differ, and `test_every_model_parameter_defaults_to_one_of_its_own_choices`
    is what keeps each of them valid.
    """
    nested = template["Parameters"]["BedrockModelId"]["Default"]
    main = main_template["Parameters"]["BedrockModelId"]["Default"]
    assert nested == main, (
        f"BedrockModelId defaults have drifted: lma-ai-stack.yaml has {nested!r}, "
        f"lma-main.yaml has {main!r}"
    )


@pytest.mark.parametrize(
    "template_name, parameter",
    [
        ("lma-ai-stack.yaml", "BedrockModelId"),
        ("lma-main.yaml", "BedrockModelId"),
        ("lma-main.yaml", "MeetingAssistServiceBedrockModelID"),
        ("lma-main.yaml", "MeetingAssistKnowledgeBaseModelID"),
    ],
)
def test_every_model_parameter_defaults_to_one_of_its_own_choices(
    template: dict, main_template: dict, template_name: str, parameter: str
):
    """A default outside its own AllowedValues fails validation on every deploy.

    It cannot be caught by deploying with the parameter set, which is how these
    are normally exercised, so it would only surface for somebody accepting the
    defaults.
    """
    source = template if template_name == "lma-ai-stack.yaml" else main_template
    spec = source["Parameters"][parameter]
    assert spec["Default"] in spec["AllowedValues"], (
        f"{template_name} {parameter} defaults to {spec['Default']!r}, "
        f"which is not one of its AllowedValues"
    )


def test_the_claude_5_models_are_selectable(template: dict, main_template: dict):
    """Both the `us.` and `global.` form of each, in both templates.

    A model offered in only one of the two regional forms is a trap for a
    deployment in a region the other form does not cover, and the pair is cheap
    to keep complete.
    """
    expected = {
        f"{prefix}.anthropic.claude-{size}-5-5"
        for prefix in ("us", "global")
        for size in ("haiku", "sonnet", "opus")
    }
    for name, source, parameters in (
        ("lma-ai-stack.yaml", template, ("BedrockModelId",)),
        ("lma-main.yaml", main_template, ("BedrockModelId", "MeetingAssistServiceBedrockModelID")),
    ):
        for parameter in parameters:
            allowed = set(source["Parameters"][parameter]["AllowedValues"])
            assert expected <= allowed, (
                f"{name} {parameter} is missing {sorted(expected - allowed)}"
            )


def test_no_claude_3_models_remain_selectable(template: dict, main_template: dict):
    """Claude 3.x was withdrawn from the model selectors in v0.3.0.

    lma-main.yaml dropped them then; this template did not, and went on offering
    eight of them. Nothing could reach them — lma-main supplies this stack's value
    — so the list was simply stale. This keeps it that way.
    """
    for name, source, parameters in (
        ("lma-ai-stack.yaml", template, ("BedrockModelId",)),
        ("lma-main.yaml", main_template, ("BedrockModelId", "MeetingAssistServiceBedrockModelID")),
    ):
        for parameter in parameters:
            stale = sorted(
                value
                for value in source["Parameters"][parameter]["AllowedValues"]
                if re.search(r"claude-3[-.]", value)
            )
            assert not stale, f"{name} {parameter} still offers withdrawn models: {stale}"


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

# Empty, and kept here as the shape of the assertion: a newly added function
# without a dedicated log group fails the test that reads this, naming itself.
# Cross-template coverage of the same rule lives in test_log_destinations.py.
FUNCTIONS_WITHOUT_A_DEDICATED_LOG_GROUP: set[str] = set()

FUNCTIONS_NOT_DEPENDING_ON_THEIR_LOG_GROUP: set[str] = set()

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


# Functions that ask for active tracing with a role that cannot write traces.
# Both predate this check. They deploy, which is the point worth knowing: Lambda
# does not refuse the function, the traces simply go nowhere, so the gap is
# invisible until somebody looks for a trace that was never recorded.
TRACED_FUNCTIONS_WHOSE_ROLE_CANNOT_WRITE_TRACES = {
    "BucketDeleteLambda",
    "UploadManifestLambda",
}


def test_a_function_that_traces_has_a_role_that_can_write_traces(resources: dict):
    """`Tracing: Active` on a declared role gets no permissions from SAM.

    SAM adds the X-Ray policy to a role it generates itself. A role declared in
    the template gets exactly what the template gives it, so a function that
    asks for tracing and a role that cannot write traces is a configuration that
    costs something and delivers nothing -- and reports nothing either.
    """
    roles = _of_type(resources, "AWS::IAM::Role")
    without = set()
    for name, resource in _functions(resources).items():
        properties = resource.get("Properties", {})
        active = (
            properties.get("Tracing") == "Active"
            or (properties.get("TracingConfig") or {}).get("Mode") == "Active"
        )
        role = properties.get("Role")
        if not active or not isinstance(role, dict) or "Fn::GetAtt" not in role:
            continue
        target = role["Fn::GetAtt"]
        target = target[0] if isinstance(target, list) else str(target).split(".")[0]
        attached = str(roles.get(target, {}).get("Properties", {}).get("ManagedPolicyArns", ""))
        if "XRay" not in attached and "Xray" not in attached:
            without.add(name)
    assert without == TRACED_FUNCTIONS_WHOSE_ROLE_CANNOT_WRITE_TRACES, (
        f"newly tracing without the permission to: "
        f"{sorted(without - TRACED_FUNCTIONS_WHOSE_ROLE_CANNOT_WRITE_TRACES)} -- give the "
        f"role AWSXRayDaemonWriteAccess or drop Tracing; now compliant: "
        f"{sorted(TRACED_FUNCTIONS_WHOSE_ROLE_CANNOT_WRITE_TRACES - without)}"
    )


def test_configuration_readers_are_ordered_behind_the_seeder(resources: dict):
    """A function must not go live before the configuration it reads exists.

    CloudFormation is free to update a Lambda before the seeding custom resource
    runs, and a reader that lands in that window has no baseline to read: a
    meeting summary with no prompts, or a configuration page showing none. The
    ordering is the fix, and it is invisible until a real upgrade hits the
    window, so it is asserted here rather than left to a deploy to discover.

    Keyed on the environment variable, so a function that starts reading
    configuration later is caught by this without anyone remembering to add it.
    """
    without = set()
    for name, resource in _functions(resources).items():
        variables = resource.get("Properties", {}).get("Environment", {}).get("Variables") or {}
        if "CONFIGURATION_TABLE_NAME" not in variables:
            continue
        declared = resource.get("DependsOn")
        declared = [declared] if isinstance(declared, str) else (declared or [])
        if "SeedConfiguration" not in declared:
            without.add(name)
    assert not without, (
        f"these functions read the configuration table but are not ordered behind "
        f"the seeder; add `SeedConfiguration` to their DependsOn: {sorted(without)}"
    )
    # Guards the selector itself: a renamed environment variable would make the
    # loop above iterate over nothing and leave this test green.
    readers = {
        name
        for name, resource in _functions(resources).items()
        if "CONFIGURATION_TABLE_NAME"
        in ((resource.get("Properties", {}).get("Environment", {}).get("Variables")) or {})
    }
    assert len(readers) >= 2, f"only found {len(readers)} functions reading the config table"


def _as_list(value) -> list:
    return value if isinstance(value, list) else [value]


def _roles_writing_the_configuration_table(resources: dict) -> set:
    """Roles granting dynamodb:PutItem on ConfigurationTable in an inline policy."""
    writers = set()
    for name, role in _of_type(resources, "AWS::IAM::Role").items():
        for policy in role.get("Properties", {}).get("Policies") or []:
            for statement in _as_list(policy.get("PolicyDocument", {}).get("Statement") or []):
                actions = _as_list(statement.get("Action") or [])
                targets = _as_list(statement.get("Resource") or [])
                if "dynamodb:PutItem" in actions and {
                    "Fn::GetAtt": "ConfigurationTable.Arn"
                } in targets:
                    writers.add(name)
    return writers


def test_every_configuration_writer_records_revisions(resources: dict):
    """A function that can save configuration must be able to record its history.

    The accessor keeps no history when it is given no bucket, which is right for
    a reader and wrong for a writer: a writer deployed without the bucket would
    save configuration that no revision holds. So every role that can write the
    table attaches the revision policy, and every function using one of those
    roles is given the bucket -- the seeder as a property of its custom
    resource, the others as CONFIGURATION_BUCKET_NAME.

    Keyed on the IAM grant rather than a list of names, so a function that
    starts writing configuration later is caught without anyone remembering it.
    """
    writers = _roles_writing_the_configuration_table(resources)
    # Guards the selector: the seeder, the five page resolvers and the history
    # resolver itself.
    assert len(writers) >= 7, f"found only {sorted(writers)}"

    without_policy = {
        name
        for name in writers
        if {"Ref": "ConfigurationRevisionWriteAccess"}
        not in (resources[name]["Properties"].get("ManagedPolicyArns") or [])
    }
    assert not without_policy, (
        f"these roles can write ConfigurationTable but do not attach "
        f"ConfigurationRevisionWriteAccess: {sorted(without_policy)}"
    )

    without_bucket = set()
    for name, function in _functions(resources).items():
        role = function.get("Properties", {}).get("Role")
        if not (isinstance(role, dict) and role.get("Fn::GetAtt", "").split(".")[0] in writers):
            continue
        if name == "ConfigurationSeederFunction":
            given = resources["SeedConfiguration"]["Properties"].get("ConfigurationBucketName")
        else:
            variables = function["Properties"].get("Environment", {}).get("Variables") or {}
            given = variables.get("CONFIGURATION_BUCKET_NAME")
        if given != {"Ref": "ConfigurationBucket"}:
            without_bucket.add(name)
    assert not without_bucket, (
        f"these functions can write ConfigurationTable but are not given "
        f"ConfigurationBucket, so their saves would record no revision: "
        f"{sorted(without_bucket)}"
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
    assert len(without) == 29, (
        f"{len(without)} of {len(roles)} IAM roles do not reference "
        f"PermissionsBoundaryArn, expected 29. If a role was added, give it the "
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
    assert arn_literals <= 88, (
        f"{arn_literals} lines hardcode 'arn:aws:' (was 88). Use "
        f'!Sub "arn:${{AWS::Partition}}:..." instead.'
    )
    assert endpoint_literals <= 84, (
        f"{endpoint_literals} lines hardcode 'amazonaws.com' (was 84). Use "
        f'!Sub "service.${{AWS::URLSuffix}}" instead.'
    )
