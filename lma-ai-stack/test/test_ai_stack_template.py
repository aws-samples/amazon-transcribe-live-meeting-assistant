# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
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

import json
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


# ── model choices ───────────────────────────────────────────────────────────
#
# The Bedrock model each feature uses is a setting of the `language_models`
# configuration section, not a stack parameter. The schema's `enum` is the list
# the Configuration page offers and the configuration API accepts, and
# setting_defaults.json holds the value a deployment starts with.

LMA_CONFIG = (
    Path(__file__).resolve().parents[1]
    / "source"
    / "lambda_layers"
    / "configuration_layer"
    / "lma_config"
)
LANGUAGE_MODELS_SCHEMA = LMA_CONFIG / "schemas" / "language_models.json"
SETTING_DEFAULTS = LMA_CONFIG / "setting_defaults.json"
LAMBDA_FUNCTIONS = Path(__file__).resolve().parents[1] / "source" / "lambda_functions"
MEETINGASSIST_SRC = Path(__file__).resolve().parents[2] / "lma-meetingassist-setup-stack" / "src"

# Every model setting in the section, so the parametrized tests below cannot pass
# by collecting nothing if one is renamed.
MODEL_SETTINGS = (
    "summaryModelId",
    "meetingAssistModelId",
    "knowledgeBaseModelId",
    "keyframeTextModelId",
)

# The settings a user picks a general-purpose model for. The knowledge-base and
# key-frame settings offer deliberately narrower lists.
GENERAL_MODEL_SETTINGS = ("summaryModelId", "meetingAssistModelId")


@pytest.fixture(scope="module")
def model_settings() -> dict:
    return json.loads(LANGUAGE_MODELS_SCHEMA.read_text())["properties"]


@pytest.fixture(scope="module")
def model_defaults() -> dict:
    return json.loads(SETTING_DEFAULTS.read_text())["language_models"]


def test_every_model_setting_is_declared_with_choices_and_a_default(
    model_settings: dict, model_defaults: dict
):
    """Guards the tests below against passing by collecting nothing."""
    for setting in MODEL_SETTINGS:
        choices = model_settings.get(setting, {}).get("enum")
        assert choices, f"language_models.json no longer offers a list of choices for {setting}"
        assert setting in model_defaults, f"setting_defaults.json has no default for {setting}"


def test_the_summary_and_meeting_assistant_settings_offer_the_same_models(model_settings: dict):
    """Both are general-purpose model choices and are meant to be the same list.

    They were the same list as stack parameters, and the defaults differ by
    design (see `test_each_model_default_matches_the_functions_fallback`), but
    what can be chosen does not. A model offered for one and not the other is
    drift, not a decision; if a model should genuinely be selectable for one
    feature only, this is the test to change deliberately.
    """
    summary = model_settings["summaryModelId"]["enum"]
    assist = model_settings["meetingAssistModelId"]["enum"]
    assert len(summary) >= 15, f"only {len(summary)} model choices — did the list move?"
    assert set(summary) == set(assist), (
        f"summaryModelId and meetingAssistModelId offer different models. "
        f"Only for summaries: {sorted(set(summary) - set(assist))}. "
        f"Only for the meeting assistant: {sorted(set(assist) - set(summary))}"
    )


def _string_constant(path: Path, name: str) -> str:
    """A module-level string constant, read from the source without importing it.

    `path` is a module, or a function directory whose handler is `index.py`.
    """
    if path.is_dir():
        path = path / "index.py"
    match = re.search(rf"^{name}\s*=\s*['\"]([^'\"]+)['\"]", path.read_text(), re.MULTILINE)
    assert match, f"{path.name} no longer declares {name}"
    return match.group(1)


# Where each model setting's default is written down again, as the fallback a
# function uses when the configuration section cannot be read.
MODEL_FALLBACKS = [
    ("summaryModelId", LAMBDA_FUNCTIONS / "bedrock_summary_lambda", "FALLBACK_MODEL_ID"),
    ("summaryModelId", LAMBDA_FUNCTIONS / "agenda_deriver", "FALLBACK_MODEL_ID"),
    ("summaryModelId", LAMBDA_FUNCTIONS / "meeting_invitation_parser", "FALLBACK_MODEL_ID"),
    ("summaryModelId", LAMBDA_FUNCTIONS / "speaker_name_resolver", "FALLBACK_MODEL_ID"),
    ("keyframeTextModelId", LAMBDA_FUNCTIONS / "keyframe_text_extractor", "FALLBACK_MODEL_ID"),
    (
        "meetingAssistModelId",
        MEETINGASSIST_SRC / "strands_meeting_assist_function.py",
        "DEFAULT_AGENT_MODEL_ID",
    ),
    (
        "meetingAssistModelId",
        MEETINGASSIST_SRC / "troubleshooting_agent_function.py",
        "FALLBACK_MODEL_ID",
    ),
]


@pytest.mark.parametrize(
    "setting, source, constant",
    MODEL_FALLBACKS,
    ids=[f"{setting}-{source.stem}" for setting, source, _ in MODEL_FALLBACKS],
)
def test_each_model_default_matches_the_functions_fallback(
    model_defaults: dict, setting: str, source: Path, constant: str
):
    """One default per setting, written down in several places, pinned so they cannot drift.

    setting_defaults.json is what an ordinary deployment starts with. Each
    function's fallback is reached only when the section cannot be read, which
    is exactly why a disagreement would go unnoticed: a function would quietly
    use a different model from the one the Configuration page shows as the
    default.

    The meeting assistant's default is deliberately not compared with the
    summary default. Summarisation is batch work where answer quality dominates,
    while the assistant answers mid-meeting and is sometimes spoken aloud, so its
    time to first token is what matters. The knowledge-base default is pinned in
    test_meeting_assist_kb_model.py.
    """
    default = model_defaults[setting]["default"]
    fallback = _string_constant(source, constant)
    assert fallback == default, (
        f"{source.stem} {constant} is {fallback!r} but setting_defaults.json "
        f"defaults {setting} to {default!r}"
    )


@pytest.mark.parametrize("setting", MODEL_SETTINGS)
def test_every_model_setting_defaults_to_one_of_its_own_choices(
    model_settings: dict, model_defaults: dict, setting: str
):
    """A default the schema does not accept is one the Configuration page cannot save back.

    It would only surface for somebody accepting the defaults, which is how most
    deployments run.
    """
    default = model_defaults[setting]["default"]
    assert default in model_settings[setting]["enum"], (
        f"setting_defaults.json defaults {setting} to {default!r}, "
        "which is not one of its choices in language_models.json"
    )


def test_the_claude_5_models_are_selectable(model_settings: dict):
    """Both the `us.` and `global.` form of each, for each general-purpose setting.

    A model offered in only one of the two regional forms is a trap for a
    deployment in a region the other form does not cover, and the pair is cheap
    to keep complete.
    """
    expected = {
        f"{prefix}.anthropic.claude-{size}-5-5"
        for prefix in ("us", "global")
        for size in ("haiku", "sonnet", "opus")
    }
    for setting in GENERAL_MODEL_SETTINGS:
        allowed = set(model_settings[setting]["enum"])
        assert expected <= allowed, f"{setting} is missing {sorted(expected - allowed)}"


def test_no_claude_3_models_remain_selectable(model_settings: dict):
    """Claude 3.x was withdrawn from the model selectors in v0.3.0; this keeps it that way."""
    for setting in MODEL_SETTINGS:
        stale = sorted(
            value for value in model_settings[setting]["enum"] if re.search(r"claude-3[-.]", value)
        )
        assert not stale, f"{setting} still offers withdrawn models: {stale}"


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


CUSTOMER_MANAGED_KEY = {"Ref": "CustomerManagedEncryptionKeyArn"}


def _role_of(function: dict) -> str | None:
    """The logical id of the role a function names with ``!GetAtt <Role>.Arn``."""
    role = (function.get("Properties") or {}).get("Role")
    if not isinstance(role, dict) or "Fn::GetAtt" not in role:
        return None
    target = role["Fn::GetAtt"]
    return target[0] if isinstance(target, list) else str(target).split(".")[0]


def _inline_statements(role: dict) -> list:
    return [
        statement
        for policy in (role.get("Properties") or {}).get("Policies") or []
        for statement in _as_list((policy.get("PolicyDocument") or {}).get("Statement") or [])
    ]


def test_a_function_that_passes_a_key_to_cloudwatch_logs_can_describe_it(resources: dict):
    """CloudWatch Logs checks a ``kmsKeyId`` given to CreateLogGroup or
    AssociateKmsKey with the caller's own permissions, so the caller needs
    ``kms:DescribeKey`` on that key. The key policy granting the Logs service
    does not cover it. Without the grant the call is denied, and a custom
    resource that runs on create (AppSyncLogRetention) fails its stack: a new
    deployment, and equally the update that first adds the resource, which an
    upgrade from v0.3.x is. A later update that leaves its properties unchanged
    does not run it, so an already-deployed stack does not show the gap.

    The grant is asserted as a plain statement on the customer-managed key: an
    ``Fn::If`` around it, or a wildcard resource, would not be found here.
    """
    roles = _of_type(resources, "AWS::IAM::Role")
    callers = {
        name: function
        for name, function in _functions(resources).items()
        if "kmsKeyId=" in str(((function.get("Properties") or {}).get("Code") or {}).get("ZipFile"))
    }
    assert "AppSyncLogRetentionFunction" in callers, "the check no longer finds its known caller"

    without = []
    for name, function in callers.items():
        role = _role_of(function)
        granted = any(
            statement.get("Effect") == "Allow"
            and "kms:DescribeKey" in _as_list(statement.get("Action") or [])
            and CUSTOMER_MANAGED_KEY in _as_list(statement.get("Resource") or [])
            for statement in _inline_statements(roles.get(role, {}))
        )
        if not granted:
            without.append(f"{name} (role {role})")
    assert not without, f"functions passing kmsKeyId without kms:DescribeKey on the key: {without}"


def test_the_appsync_log_group_is_encrypted_with_the_key_its_role_can_describe(resources: dict):
    """The key the custom resource hands its function is the one the role's grant
    names; a different key would be denied just the same."""
    properties = resources["AppSyncLogRetention"]["Properties"]
    assert properties["KmsKeyId"] == CUSTOMER_MANAGED_KEY
    assert properties["ServiceToken"] == {"Fn::GetAtt": "AppSyncLogRetentionFunction.Arn"}


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


def test_the_web_ui_is_built_after_the_graphql_schema(resources: dict):
    """The UI's queries name fields the schema in the same update adds, and AppSync
    refuses a query that names a field its schema lacks. Ordering the UI build
    behind the schema keeps an update from serving a page whose queries fail."""
    assert resources["AppSyncSchema"]["Type"] == "AWS::AppSync::GraphQLSchema"
    assert "Condition" not in resources["AppSyncSchema"]
    build = resources["CodeBuildRun"]
    assert build["Properties"]["BuildProjectName"] == {"Ref": "LMAUICodeBuildProject"}
    declared = build.get("DependsOn")
    declared = [declared] if isinstance(declared, str) else (declared or [])
    assert "AppSyncSchema" in declared


def test_the_upload_initiator_can_read_the_transcription_section(resources: dict):
    """An upload follows the Configuration page's Amazon Transcribe language, read
    by the initiator when the upload starts. Without the table name the function
    uses the shipped en-US; without the layer it cannot import; without the grant
    every upload is refused because the settings cannot be read."""
    function = resources["UploadMeetingInitiatorFunction"]
    properties = function["Properties"]
    assert properties["Environment"]["Variables"]["CONFIGURATION_TABLE_NAME"] == {
        "Ref": "ConfigurationTable"
    }
    assert {"Ref": "ConfigurationPythonLayer"} in properties["Layers"]
    role = resources[properties["Role"]["Fn::GetAtt"].split(".")[0]]
    statements = [
        statement
        for policy in role["Properties"]["Policies"]
        for statement in policy["PolicyDocument"]["Statement"]
    ]
    readers = [
        statement
        for statement in statements
        if statement.get("Resource") == {"Fn::GetAtt": "ConfigurationTable.Arn"}
    ]
    assert [statement["Action"] for statement in readers] == [["dynamodb:GetItem"]]
    kms = [
        statement
        for statement in statements
        if "kms:Decrypt" in statement.get("Action", [])
        and statement.get("Resource") == {"Ref": "CustomerManagedEncryptionKeyArn"}
    ]
    assert kms, "the configuration table is encrypted with the stack's key"
    via = json.dumps(kms[0].get("Condition"))
    assert "dynamodb." in via


def test_the_asr_engine_resolvers_exist_without_the_on_demand_engine(resources: dict):
    """getAsrConfig is how a Virtual Participant reads its voice separation switch.

    That switch is honoured on Amazon Transcribe as well, so a deployment without
    the on-demand engine still needs the read; with no resolver attached AppSync
    answers the field with null and the VP reads the switch as off.
    """
    chain = [
        "UpdateAsrConfigResolverFunction",
        "UpdateAsrConfigResolverFunctionLogGroup",
        "UpdateAsrConfigResolverFunctionExecutionRole",
        "AppSyncUpdateAsrConfigLambdaRole",
        "UpdateAsrConfigLambdaDataSource",
        "GetAsrConfigResolver",
        "UpdateAsrConfigResolver",
    ]
    conditional = {
        name: resources[name].get("Condition") for name in chain if resources[name].get("Condition")
    }
    assert not conditional, f"these are created only under a condition: {conditional}"


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


# AppSync's Velocity parser rejects a method call made directly on a list
# literal (for example ``["a", "b"].contains($x)``) with a "Lexical error" at
# request time, which cfn-lint and SAM validation do not catch. Assign the
# list to a variable first.
_LIST_LITERAL_CALL = re.compile(r"\]\s*\.\s*[A-Za-z_]+\s*\(")


@pytest.mark.parametrize("path", [TEMPLATE, MAIN_TEMPLATE])
def test_no_velocity_method_call_on_a_list_literal(path: Path):
    offenders = [
        f"{path.name}:{number}: {line.strip()}"
        for number, line in enumerate(path.read_text().splitlines(), start=1)
        if line.lstrip().startswith("#") and _LIST_LITERAL_CALL.search(line)
    ]
    assert not offenders, "assign the list to a #set variable first:\n" + "\n".join(offenders)
