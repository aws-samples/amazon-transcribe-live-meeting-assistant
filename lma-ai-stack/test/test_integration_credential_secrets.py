# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Static tests for how operator-supplied third-party credentials are carried.

Five credentials are entered as CloudFormation parameters on `lma-main.yaml`
and consumed by two other stacks: the Virtual Participant's ECS task (the Zoom
Meeting SDK client secret, the Azure Communication Services connection string,
the ElevenLabs API key and the Simli API key) and the meeting assist Lambda
(the Tavily API key).

`lma-main.yaml` stores each one in a Secrets Manager secret of its own and
passes the nested stacks the secret's ARN. The task definition names the ARN in
its `Secrets` list, which the ECS agent resolves with the task EXECUTION role
at task start; the Lambda is given the ARN in its environment and fetches the
value itself.

The invariants below are the ones that a later edit could quietly undo — adding
an `Environment` entry back onto the task definition, widening an IAM grant to
`*`, or granting the read to the task role (which the ECS agent does not use
for this) instead of the execution role. All three would still deploy and still
work, which is why they are asserted here.

Run via `make test-ai-stack` or pytest directly.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
MAIN_TEMPLATE = REPO / "lma-main.yaml"
VP_TEMPLATE = REPO / "lma-virtual-participant-stack" / "template.yaml"
MA_TEMPLATE = REPO / "lma-meetingassist-setup-stack" / "template.yaml"

# credential parameter -> (secret resource in lma-main.yaml, secret name suffix)
CREDENTIALS = {
    "TavilyApiKey": ("TavilyApiKeySecret", "tavily-api-key"),
    "ElevenLabsApiKey": ("ElevenLabsApiKeySecret", "elevenlabs-api-key"),
    "SimliApiKey": ("SimliApiKeySecret", "simli-api-key"),
    "ZoomMeetingSdkClientSecret": (
        "ZoomMeetingSdkClientSecretSecret",
        "zoom-meeting-sdk-client-secret",
    ),
    "AcsConnectionString": ("AcsConnectionStringSecret", "acs-connection-string"),
}

# The four the Virtual Participant container consumes, as
# container environment variable -> the VP stack parameter carrying the ARN.
VP_CONTAINER_SECRETS = {
    "ZOOM_MEETING_SDK_CLIENT_SECRET": "ZoomMeetingSdkClientSecretArn",
    "ACS_CONNECTION_STRING": "AcsConnectionStringSecretArn",
    "ELEVENLABS_API_KEY": "ElevenLabsApiKeySecretArn",
    "SIMLI_API_KEY": "SimliApiKeySecretArn",
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


def _load(path: Path) -> dict:
    return yaml.load(path.read_text(), Loader=_CfnLoader)


@pytest.fixture(scope="module")
def main() -> dict:
    return _load(MAIN_TEMPLATE)


@pytest.fixture(scope="module")
def vp() -> dict:
    return _load(VP_TEMPLATE)


@pytest.fixture(scope="module")
def meetingassist() -> dict:
    return _load(MA_TEMPLATE)


def _refs(node) -> set[str]:
    """Every parameter or resource name reached by a Ref under `node`.

    Pseudo parameters are dropped: `!Ref AWS::NoValue` is how the unused branch
    of an `!If` removes an item, and it is never the subject of an assertion
    here.
    """
    found: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "Ref" and isinstance(value, str):
                found.add(value)
            elif key == "Fn::GetAtt" and isinstance(value, str):
                found.add(value.split(".")[0])
            elif key == "Fn::GetAtt" and isinstance(value, list) and value:
                found.add(str(value[0]))
            else:
                found |= _refs(value)
    elif isinstance(node, list):
        for item in node:
            found |= _refs(item)
    return {name for name in found if not name.startswith("AWS::")}


def _statements(policy_documents) -> list[dict]:
    out: list[dict] = []
    for document in policy_documents:
        for statement in document.get("Statement", []):
            if isinstance(statement, dict):
                # An `!If`-wrapped statement: both branches are candidates.
                if "Fn::If" in statement:
                    out.extend(b for b in statement["Fn::If"][1:] if isinstance(b, dict))
                else:
                    out.append(statement)
    return out


def _policy_documents(role: dict) -> list[dict]:
    policies = role.get("Properties", {}).get("Policies", [])
    if isinstance(policies, dict) and "Fn::If" in policies:
        branches = [b for b in policies["Fn::If"][1:] if isinstance(b, list)]
        policies = [item for branch in branches for item in branch]
    documents = []
    for policy in policies:
        if isinstance(policy, dict) and "Fn::If" in policy:
            candidates = [b for b in policy["Fn::If"][1:] if isinstance(b, dict)]
        else:
            candidates = [policy] if isinstance(policy, dict) else []
        documents.extend(c["PolicyDocument"] for c in candidates if "PolicyDocument" in c)
    return documents


def _actions(statement: dict) -> set[str]:
    action = statement.get("Action", [])
    return {action} if isinstance(action, str) else set(action)


def _resources(statement: dict) -> list:
    resource = statement.get("Resource", [])
    return [resource] if not isinstance(resource, list) else resource


# ---------------------------------------------------------------------------
# lma-main.yaml: one secret per credential
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("parameter", sorted(CREDENTIALS))
def test_each_credential_parameter_is_stored_in_a_secret(parameter: str, main: dict):
    """The parameter is still how an operator supplies the value."""
    assert parameter in main["Parameters"], f"{parameter} is no longer a parameter"
    assert main["Parameters"][parameter].get("NoEcho") is True

    resource_name, _ = CREDENTIALS[parameter]
    secret = main["Resources"][resource_name]
    assert secret["Type"] == "AWS::SecretsManager::Secret"
    assert secret["Properties"]["SecretString"] == {"Ref": parameter}


@pytest.mark.parametrize("parameter", sorted(CREDENTIALS))
def test_each_secret_is_created_only_when_its_credential_is_supplied(
    parameter: str, main: dict
):
    """Several of these features are off by default and must create no secret."""
    resource_name, _ = CREDENTIALS[parameter]
    condition = main["Resources"][resource_name].get("Condition")
    assert condition, f"{resource_name} has no Condition and would always be created"
    assert condition in main["Conditions"]
    assert main["Conditions"][condition] == {
        "Fn::Not": [{"Fn::Equals": [{"Ref": parameter}, ""]}]
    }


@pytest.mark.parametrize("parameter", sorted(CREDENTIALS))
def test_each_secret_is_encrypted_with_the_stacks_customer_managed_key(
    parameter: str, main: dict
):
    resource_name, _ = CREDENTIALS[parameter]
    properties = main["Resources"][resource_name]["Properties"]
    assert properties["KmsKeyId"] == {"Fn::GetAtt": "CustomerManagedEncryptionKey.Arn"}


@pytest.mark.parametrize("parameter", sorted(CREDENTIALS))
def test_each_secret_is_named_under_the_integrations_prefix(parameter: str, main: dict):
    """A findable name is what makes the rotation procedure in the docs usable."""
    resource_name, suffix = CREDENTIALS[parameter]
    properties = main["Resources"][resource_name]["Properties"]
    assert properties["Name"] == {
        "Fn::Sub": f"${{AWS::StackName}}/integrations/{suffix}"
    }


@pytest.mark.parametrize("parameter", sorted(CREDENTIALS))
def test_each_secret_states_its_deletion_policy(parameter: str, main: dict):
    """Delete, not the EnableDataRetentionOnDelete conditional used for data.

    The value is an operator input that also exists at the provider and in the
    stack parameter, and the secret carries an explicit name that a retained
    copy would hold indefinitely against a redeployment of the same stack name.
    Stated explicitly rather than left to the default so the choice is visible.
    """
    resource_name, _ = CREDENTIALS[parameter]
    resource = main["Resources"][resource_name]
    assert resource.get("DeletionPolicy") == "Delete"
    assert resource.get("UpdateReplacePolicy") == "Delete"


@pytest.mark.parametrize("parameter", sorted(CREDENTIALS))
def test_both_forms_of_each_credential_cannot_be_supplied_at_once(
    parameter: str, main: dict
):
    """Each credential also accepts the ARN of a secret the operator provisioned."""
    arn_parameter = (
        "ZoomMeetingSdkClientSecretArn"
        if parameter == "ZoomMeetingSdkClientSecret"
        else f"{parameter}SecretArn"
    )
    assert arn_parameter in main["Parameters"]

    rules = main["Rules"]
    matching = [
        rule
        for rule in rules.values()
        if rule.get("RuleCondition") == {"Fn::Not": [{"Fn::Equals": [{"Ref": parameter}, ""]}]}
    ]
    assert matching, f"no Rule rejects {parameter} together with {arn_parameter}"
    asserted = {
        tuple(sorted(_refs(assertion["Assert"])))
        for rule in matching
        for assertion in rule["Assertions"]
    }
    assert (arn_parameter,) in asserted


def test_no_nested_stack_is_passed_a_credential_in_plaintext(main: dict):
    """What travels to the nested stacks is an ARN, in both of the two forms."""
    offenders = []
    for name, resource in main["Resources"].items():
        if resource.get("Type") != "AWS::CloudFormation::Stack":
            continue
        for key, value in resource.get("Properties", {}).get("Parameters", {}).items():
            leaked = _refs(value) & set(CREDENTIALS)
            if leaked:
                offenders.append(f"{name}.{key} -> {sorted(leaked)}")
    assert not offenders, (
        "nested stack parameters carry a credential value rather than a secret "
        f"ARN: {offenders}"
    )


# ---------------------------------------------------------------------------
# Virtual Participant: injected by the ECS agent, read by the execution role
# ---------------------------------------------------------------------------


def test_the_task_definition_names_no_credential_parameter_in_its_environment(vp: dict):
    """An `Environment` entry would put the value in the task definition itself."""
    container = vp["Resources"]["TaskDefinition"]["Properties"]["ContainerDefinitions"][0]
    forbidden = set(CREDENTIALS) | set(VP_CONTAINER_SECRETS.values())
    offenders = [
        entry.get("Name")
        for entry in container["Environment"]
        if isinstance(entry, dict) and _refs(entry) & forbidden
    ]
    assert not offenders, f"task definition Environment entries carry a secret: {offenders}"

    # And the old plaintext parameters are gone from the stack altogether.
    assert not set(CREDENTIALS) & set(vp["Parameters"]), (
        "the Virtual Participant stack still declares a plaintext credential "
        f"parameter: {sorted(set(CREDENTIALS) & set(vp['Parameters']))}"
    )


def test_the_task_definition_injects_each_credential_from_its_secret(vp: dict):
    container = vp["Resources"]["TaskDefinition"]["Properties"]["ContainerDefinitions"][0]
    branches = container["Secrets"]["Fn::If"][1:]
    entries = [item for branch in branches if isinstance(branch, list) for item in branch]
    injected = {}
    for entry in entries:
        for candidate in entry.get("Fn::If", [])[1:] if "Fn::If" in entry else [entry]:
            if isinstance(candidate, dict) and "Name" in candidate:
                injected[candidate["Name"]] = candidate["ValueFrom"]
    assert injected == {
        name: {"Ref": parameter} for name, parameter in VP_CONTAINER_SECRETS.items()
    }


def test_the_task_execution_role_reads_the_secrets_scoped_to_their_arns(vp: dict):
    """The ECS agent uses the execution role, so the grant belongs there."""
    role = vp["Resources"]["TaskExecutionRole"]
    statements = _statements(_policy_documents(role))
    reads = [s for s in statements if "secretsmanager:GetSecretValue" in _actions(s)]
    assert reads, "TaskExecutionRole is granted no secret read"

    granted: set[str] = set()
    for statement in reads:
        for resource in _resources(statement):
            assert resource != "*", "the secret read is not scoped to an ARN"
            granted |= _refs(resource)
    assert granted == set(VP_CONTAINER_SECRETS.values())

    decrypts = [s for s in statements if "kms:Decrypt" in _actions(s)]
    assert decrypts, "TaskExecutionRole cannot decrypt the secrets it is allowed to read"
    assert all(
        "CustomerManagedEncryptionKeyArn" in _refs(_resources(s)) for s in decrypts
    )


def test_the_task_role_is_not_the_one_granted_the_integration_secrets(vp: dict):
    """The distinction is easy to get wrong and works either way at deploy time.

    The container's own credentials (TaskRole) are not what the ECS agent uses
    to resolve a `Secrets` entry, so granting them here would be a widening with
    no purpose. TaskRole's other Secrets Manager grant — the per-user Zoom
    credentials it reads at run time — is left alone.
    """
    role = vp["Resources"]["TaskRole"]
    referenced = _refs(_policy_documents(role))
    assert not referenced & set(VP_CONTAINER_SECRETS.values()), (
        "TaskRole references an integration secret ARN: "
        f"{sorted(referenced & set(VP_CONTAINER_SECRETS.values()))}"
    )


def test_the_microvm_launcher_reads_the_same_secrets_scoped_to_their_arns(vp: dict):
    """A MicroVM has no ECS agent, so the launcher resolves the same entries.

    Asserted the same way as the task execution role above, decrypt grant
    included, so the two roles that read these secrets cannot drift apart.
    """
    role = vp["Resources"]["VPMicrovmLauncherRole"]
    statements = _statements(_policy_documents(role))
    reads = [s for s in statements if "secretsmanager:GetSecretValue" in _actions(s)]
    assert reads, "the MicroVM launcher is granted no secret read"
    granted: set[str] = set()
    for statement in reads:
        for resource in _resources(statement):
            assert resource != "*", "the secret read is not scoped to an ARN"
            granted |= _refs(resource)
    assert granted == set(VP_CONTAINER_SECRETS.values())

    # The secrets are encrypted with the customer-managed key, so a read needs
    # a decrypt grant through Secrets Manager as well.
    decrypts = [
        s
        for s in statements
        if "kms:Decrypt" in _actions(s)
        and "secretsmanager"
        in str(s.get("Condition", {}).get("StringEquals", {}).get("kms:ViaService", ""))
    ]
    assert decrypts, (
        "the MicroVM launcher can read the secrets but cannot decrypt them: no "
        "kms:Decrypt statement conditioned on kms:ViaService for Secrets Manager"
    )
    assert all(
        "CustomerManagedEncryptionKeyArn" in _refs(_resources(s)) for s in decrypts
    )


def test_the_microvm_launcher_resolves_the_task_definitions_secrets(vp: dict):
    """The Lambda side of the same path, asserted on its source.

    Under MICROVM the container is handed its static configuration by the
    launcher rather than by the ECS agent, so the launcher is what turns a
    `Secrets` entry into an environment variable. Its behaviour is tested in
    lma-virtual-participant-stack/test/test_microvm_launcher_secrets.py; this
    is the template-side check that the two have not drifted apart.
    """
    source = (
        VP_TEMPLATE.parent / "lambda_functions" / "microvm_launcher" / "index.py"
    ).read_text()
    assert "_integration_secrets" in source
    assert "get_secret_value" in source
    for name in VP_CONTAINER_SECRETS:
        assert name in source, (
            f"the task definition injects {name}, but the MicroVM launcher does "
            f"not name it, so that launch type would not supply it"
        )


# ---------------------------------------------------------------------------
# Meeting assist: fetched by the function, from the ARN in its environment
# ---------------------------------------------------------------------------


def test_the_meeting_assist_function_is_given_the_arn_not_the_key(meetingassist: dict):
    function = meetingassist["Resources"]["StrandsMeetingAssistFunction"]
    variables = function["Properties"]["Environment"]["Variables"]
    assert "TAVILY_API_KEY" not in variables
    assert variables["TAVILY_API_KEY_SECRET_ARN"] == {"Ref": "TavilyApiKeySecretArn"}
    assert "TavilyApiKey" not in meetingassist["Parameters"]


def test_the_meeting_assist_role_reads_only_that_one_secret(meetingassist: dict):
    role = meetingassist["Resources"]["StrandsMeetingAssistFunctionRole"]
    statements = _statements(_policy_documents(role))
    reads = [s for s in statements if "secretsmanager:GetSecretValue" in _actions(s)]
    assert reads, "the meeting assist function is granted no secret read"
    granted: set[str] = set()
    for statement in reads:
        for resource in _resources(statement):
            assert resource != "*", "the secret read is not scoped to an ARN"
            granted |= _refs(resource)
    assert granted == {"TavilyApiKeySecretArn"}
