# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""The recordings bucket and the call data stream use the stack's customer-managed key.

Checked statically (no AWS):

- `RecordingsBucket` in `lma-main.yaml` encrypts with SSE-KMS under
  `CustomerManagedEncryptionKey`, with an S3 Bucket Key;
- `CallDataStream` encrypts with the same key;
- every role in the AI stack whose policies name the bucket (`S3BucketName`)
  or the stream (`CallDataStream`/`CallDataStreamArn`) is attached to the
  matching key policy, and the Kinesis consumer is attached too;
- the transcriber task role, the Virtual Participant task role (used by the ECS
  tasks and the MicroVM), the web UI's Cognito role and the knowledge base
  service role can use the key through Amazon S3 (and Kinesis, for the
  producers);
- batch Amazon Transcribe jobs name the key for their output;
- every custom resource whose function role is attached to one of the key
  policies `DependsOn` that policy.

Run via `make test-ai-stack` or pytest directly.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
MAIN = REPO_ROOT / "lma-main.yaml"
AI_STACK = REPO_ROOT / "lma-ai-stack" / "deployment" / "lma-ai-stack.yaml"
TRANSCRIBER = (
    REPO_ROOT / "lma-websocket-transcriber-stack" / "deployment" / "lma-websocket-transcriber.yaml"
)
VP = REPO_ROOT / "lma-virtual-participant-stack" / "template.yaml"
COGNITO = REPO_ROOT / "lma-cognito-stack" / "deployment" / "lma-cognito-stack.yaml"
BEDROCK_KB = REPO_ROOT / "lma-bedrockkb-stack" / "template.yaml"

CMK_ARN = {"Fn::GetAtt": "CustomerManagedEncryptionKey.Arn"}
NESTED_CMK = {"Ref": "CustomerManagedEncryptionKeyArn"}
S3_VIA = {"Fn::Sub": "s3.${AWS::Region}.${AWS::URLSuffix}"}
KINESIS_VIA = {"Fn::Sub": "kinesis.${AWS::Region}.${AWS::URLSuffix}"}


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

_CACHE: dict[Path, dict] = {}


def _resources(path: Path) -> dict:
    if path not in _CACHE:
        _CACHE[path] = yaml.load(path.read_text(), Loader=_CfnLoader)
    return _CACHE[path]["Resources"]


def _statements(node) -> list[dict]:
    """Every IAM statement under a node, through !If branches."""
    found: list[dict] = []

    def walk(value) -> None:
        if isinstance(value, dict):
            if "Effect" in value and "Action" in value:
                found.append(value)
                return
            for key, child in value.items():
                if key in ("AssumeRolePolicyDocument", "Environment", "Events"):
                    continue
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(node)
    return found


def _actions(statement: dict) -> set[str]:
    actions = statement["Action"]
    return set(actions if isinstance(actions, list) else [actions])


def _via(statement: dict):
    return (statement.get("Condition") or {}).get("StringEquals", {}).get("kms:ViaService")


def _key_statements(node, key, via) -> list[dict]:
    return [
        s
        for s in _statements(node)
        if s.get("Resource") == key
        and _via(s) == via
        and any(a.startswith("kms:") for a in _actions(s))
    ]


# ── the resources themselves ────────────────────────────────────────────────


def test_recordings_bucket_is_encrypted_with_the_customer_managed_key():
    encryption = _resources(MAIN)["RecordingsBucket"]["Properties"]["BucketEncryption"]
    (rule,) = encryption["ServerSideEncryptionConfiguration"]
    assert rule["ServerSideEncryptionByDefault"] == {
        "SSEAlgorithm": "aws:kms",
        "KMSMasterKeyID": CMK_ARN,
    }
    assert rule["BucketKeyEnabled"] is True


def test_call_data_stream_is_encrypted_with_the_customer_managed_key():
    encryption = _resources(MAIN)["CallDataStream"]["Properties"]["StreamEncryption"]
    assert encryption == {"EncryptionType": "KMS", "KeyId": CMK_ARN}


def test_key_policy_lets_transcribe_use_the_key_within_the_account():
    key_policy = _resources(MAIN)["CustomerManagedEncryptionKey"]["Properties"]["KeyPolicy"]
    (statement,) = [
        s
        for s in key_policy["Statement"]
        if s["Principal"].get("Service") == {"Fn::Sub": "transcribe.${AWS::URLSuffix}"}
    ]
    assert {"kms:Decrypt", "kms:GenerateDataKey*"} <= _actions(statement)
    assert statement["Condition"] == {
        "StringEquals": {"aws:SourceAccount": {"Ref": "AWS::AccountId"}}
    }


# ── the AI stack's roles ────────────────────────────────────────────────────


def _role_ref(name: str, resource: dict) -> str:
    """The role a policy holder's grants land on, as the logical ID to !Ref."""
    if resource["Type"] == "AWS::Serverless::Function":
        role = resource["Properties"].get("Role")
        assert role is None, f"{name} names its role; check that role instead"
        return f"{name}Role"  # the role SAM generates
    return name


def _holders_naming(*needles: str) -> set[str]:
    holders = set()
    for name, resource in _resources(AI_STACK).items():
        if resource["Type"] not in ("AWS::IAM::Role", "AWS::Serverless::Function"):
            continue
        properties = resource.get("Properties", {})
        if resource["Type"] == "AWS::Serverless::Function":
            # Statements and SAM policy templates (S3ReadPolicy and the like).
            blob = json.dumps(properties.get("Policies", []))
        else:
            blob = json.dumps(_statements(properties))
        if any(needle in blob for needle in needles):
            holders.add(_role_ref(name, resource))
    return holders


def _attached_roles(policy_name: str) -> set[str]:
    roles = set()
    for entry in _resources(AI_STACK)[policy_name]["Properties"]["Roles"]:
        if "Fn::If" in entry:
            entry = entry["Fn::If"][1]
        roles.add(entry["Ref"])
    return roles


def _inline_covered(via) -> set[str]:
    """SAM functions whose own Policies grant the key through `via` (or unscoped)."""
    covered = set()
    for name, resource in _resources(AI_STACK).items():
        if resource["Type"] != "AWS::Serverless::Function":
            continue
        for statement in _statements(resource["Properties"].get("Policies", [])):
            if statement.get("Resource") != NESTED_CMK:
                continue
            uses_key = _actions(statement) & {"kms:Decrypt", "kms:GenerateDataKey"}
            if _via(statement) in (via, None) and uses_key:
                covered.add(f"{name}Role")
    return covered


def test_the_bucket_and_stream_users_are_found():
    assert "UploadMeetingProcessorFunctionExecutionRole" in _holders_naming("S3BucketName")
    assert "MCPServerAnalyticsFunctionRole" in _holders_naming("S3BucketName")
    assert "StaleMeetingReaperFunctionExecutionRole" in _holders_naming("CallDataStream")
    assert "VirtualParticipantManagerFunctionRole" in _holders_naming("CallDataStream")


def test_every_ai_stack_role_using_the_bucket_can_use_its_key():
    covered = _attached_roles("RecordingsBucketEncryptionKeyPolicy") | _inline_covered(S3_VIA)
    missing = _holders_naming("S3BucketName") - covered
    assert not missing, f"add to RecordingsBucketEncryptionKeyPolicy.Roles: {sorted(missing)}"


def test_every_ai_stack_role_using_the_stream_can_use_its_key():
    # The consumer reads through DataStreamConsumer, which its policies do not name.
    expected = _holders_naming("CallDataStream") | {"CallEventProcessorFunctionRole"}
    covered = _attached_roles("CallDataStreamEncryptionKeyPolicy") | _inline_covered(KINESIS_VIA)
    missing = expected - covered
    assert not missing, f"add to CallDataStreamEncryptionKeyPolicy.Roles: {sorted(missing)}"


def test_shared_key_policies_name_only_declared_roles():
    resources = _resources(AI_STACK)
    for policy in ("RecordingsBucketEncryptionKeyPolicy", "CallDataStreamEncryptionKeyPolicy"):
        for role in _attached_roles(policy):
            assert resources[role]["Type"] == "AWS::IAM::Role", (policy, role)


def test_ai_stack_key_grants_are_scoped_to_the_service():
    bucket = _resources(AI_STACK)["RecordingsBucketEncryptionKeyPolicy"]["Properties"]
    (s3,) = _key_statements(bucket["PolicyDocument"], NESTED_CMK, S3_VIA)
    assert {"kms:Decrypt", "kms:GenerateDataKey"} <= _actions(s3)
    stream = _resources(AI_STACK)["CallDataStreamEncryptionKeyPolicy"]["Properties"]
    (kinesis,) = _key_statements(stream["PolicyDocument"], NESTED_CMK, KINESIS_VIA)
    assert {"kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey"} <= _actions(kinesis)


def test_batch_transcription_output_names_the_key():
    variables = _resources(AI_STACK)["UploadMeetingProcessorFunction"]["Properties"]["Environment"][
        "Variables"
    ]
    assert variables["OUTPUT_ENCRYPTION_KMS_KEY_ID"] == NESTED_CMK


# ── the other stacks' roles ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "template, resource, via, actions",
    [
        (
            TRANSCRIBER,
            "TranscriberWebsocketTaskDefTaskRoleDefaultPolicy",
            S3_VIA,
            {"kms:GenerateDataKey", "kms:Decrypt"},
        ),
        (
            TRANSCRIBER,
            "TranscriberWebsocketTaskDefTaskRoleDefaultPolicy",
            KINESIS_VIA,
            {"kms:GenerateDataKey"},
        ),
        (VP, "TaskRole", S3_VIA, {"kms:GenerateDataKey", "kms:Decrypt"}),
        (VP, "TaskRole", KINESIS_VIA, {"kms:GenerateDataKey"}),
        (COGNITO, "CognitoAuthorizedRole", S3_VIA, {"kms:Decrypt"}),
        (BEDROCK_KB, "KnowledgeBaseServiceRole", S3_VIA, {"kms:Decrypt"}),
    ],
)
def test_role_can_use_the_key_through_the_service(template, resource, via, actions):
    resources = _resources(template)
    if resource not in resources:  # the knowledge base role's logical ID
        (resource,) = [
            n
            for n, r in resources.items()
            if r["Type"] == "AWS::IAM::Role"
            and "bedrock.amazonaws.com" in json.dumps(r["Properties"]["AssumeRolePolicyDocument"])
        ]
    statements = _key_statements(resources[resource]["Properties"], NESTED_CMK, via)
    assert statements, f"{resource} has no key grant through {via}"
    assert actions <= set().union(*(_actions(s) for s in statements))


def test_web_ui_key_grant_is_decrypt_only_for_this_bucket():
    role = _resources(COGNITO)["CognitoAuthorizedRole"]["Properties"]
    (statement,) = _key_statements(role, NESTED_CMK, S3_VIA)
    assert _actions(statement) == {"kms:Decrypt"}
    context = statement["Condition"]["StringLike"]["kms:EncryptionContext:aws:s3:arn"]
    assert len(context) == 2


def test_only_the_upload_processor_uses_the_key_through_transcribe():
    """Only the role that starts batch Transcribe jobs uses the key through Transcribe."""
    resources = _resources(AI_STACK)
    holders = []
    for name, resource in resources.items():
        if resource.get("Type") != "AWS::IAM::Policy":
            continue
        for statement in resource["Properties"]["PolicyDocument"]["Statement"]:
            via = (statement.get("Condition") or {}).get("StringEquals", {}).get("kms:ViaService")
            if via == {"Fn::Sub": "transcribe.${AWS::Region}.${AWS::URLSuffix}"}:
                holders.append((name, resource["Properties"]["Roles"]))
    assert holders == [
        ("TranscribeOutputEncryptionKeyPolicy", [{"Ref": "UploadMeetingProcessorFunctionExecutionRole"}])
    ]


def _custom_resource_roles(resources: dict) -> dict[str, str]:
    """Custom resource name -> the role of the function that serves it.

    Only functions that name a role with ``Role: !GetAtt <Role>.Arn`` are
    resolved. A SAM function with a generated role (the configuration seeder,
    the backfill trigger) is skipped on purpose: such a role carries its key
    grant inline, so it has no separate key policy to wait for.
    """
    found: dict[str, str] = {}
    for name, res in resources.items():
        if not str(res.get("Type", "")).startswith("Custom::"):
            continue
        token = (res.get("Properties") or {}).get("ServiceToken")
        if not (isinstance(token, dict) and "Fn::GetAtt" in token):
            continue
        target = token["Fn::GetAtt"]
        function = target.split(".")[0] if isinstance(target, str) else target[0]
        role = (resources.get(function, {}).get("Properties") or {}).get("Role")
        if isinstance(role, dict) and "Fn::GetAtt" in role:
            arn = role["Fn::GetAtt"]
            found[name] = arn.split(".")[0] if isinstance(arn, str) else arn[0]
    return found


def test_custom_resources_wait_for_their_roles_key_policy():
    resources = _resources(AI_STACK)
    policies = ("RecordingsBucketEncryptionKeyPolicy", "CallDataStreamEncryptionKeyPolicy")
    for name, role in _custom_resource_roles(resources).items():
        depends = resources[name].get("DependsOn") or []
        depends = {depends} if isinstance(depends, str) else set(depends)
        for policy in policies:
            if role in _attached_roles(policy):
                assert policy in depends, f"{name} must DependsOn {policy}"


def test_quicksight_manifest_custom_resource_is_checked():
    assert _custom_resource_roles(_resources(AI_STACK))["UploadQuickSightManifest"] == (
        "UploadManifestLambdaRole"
    )
