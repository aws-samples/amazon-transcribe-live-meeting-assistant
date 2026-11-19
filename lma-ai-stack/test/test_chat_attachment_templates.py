# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""What the templates grant and keep for chat attachments (no AWS).

Chat attachments are files in the recordings bucket under
``lma-chat-attachments/<meeting>/`` and items under ``att#<CallId>``; see
docs/virtual-participant.md, "Chat attachments". Pinned here:

* the two functions that read the files (the summarizer and the resolver that
  signs links) read that folder and nothing else in the bucket, and only the
  attachment items (and, for the resolver, the meeting row) in the table;
* the Virtual Participant's task role can write objects into the bucket, the
  transcript deletion can remove every version under the folder, and the call
  event processor can queue the summarizer;
* the files are kept no longer than the transcript.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterator, List

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
AI_STACK = REPO / "lma-ai-stack" / "deployment" / "lma-ai-stack.yaml"
MAIN = REPO / "lma-main.yaml"
VP = REPO / "lma-virtual-participant-stack" / "template.yaml"
PREFIX = "lma-chat-attachments/"


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


def _resources(path: Path) -> Dict[str, Any]:
    return yaml.load(path.read_text(encoding="utf-8"), Loader=_CfnLoader)["Resources"]


@pytest.fixture(scope="module")
def ai() -> Dict[str, Any]:
    return _resources(AI_STACK)


def _statements(role: Dict[str, Any]) -> Iterator[Dict[str, Any]]:
    for policy in role["Properties"].get("Policies", []):
        if not isinstance(policy, dict) or "PolicyDocument" not in policy:
            continue
        yield from policy["PolicyDocument"]["Statement"]


def _actions(statement: Dict[str, Any]) -> List[str]:
    action = statement["Action"]
    return [action] if isinstance(action, str) else list(action)


def _resources_of(statement: Dict[str, Any]) -> List[str]:
    resource = statement["Resource"]
    resources = resource if isinstance(resource, list) else [resource]
    return [json.dumps(r) for r in resources]


def _s3_statements(role):
    return [s for s in _statements(role) if any(a.startswith("s3:") for a in _actions(s))]


@pytest.mark.parametrize(
    "role", ["ChatAttachmentSummarizerFunctionExecutionRole", "ChatAttachmentsResolverFunctionExecutionRole"]
)
def test_the_readers_of_the_files_read_the_attachments_folder_only(ai, role):
    statements = _s3_statements(ai[role])
    assert statements, role
    for statement in statements:
        assert _actions(statement) == ["s3:GetObject"], role
        for resource in _resources_of(statement):
            assert f"{PREFIX}*" in resource, (role, resource)


@pytest.mark.parametrize(
    "role, keys",
    [
        ("ChatAttachmentSummarizerFunctionExecutionRole", {"att#*", "mtr#*"}),
        ("ChatAttachmentsResolverFunctionExecutionRole", {"att#*", "c#*"}),
    ],
)
def test_the_readers_of_the_items_are_held_to_their_partitions(ai, role, keys):
    granted = set()
    for statement in _statements(ai[role]):
        if not any(a.startswith("dynamodb:") for a in _actions(statement)):
            continue
        if "ConfigurationTable" in json.dumps(statement["Resource"]):
            assert _actions(statement) == ["dynamodb:GetItem"]
            continue
        leading = statement["Condition"]["ForAllValues:StringLike"]["dynamodb:LeadingKeys"]
        granted |= set(leading)
        assert "dynamodb:DeleteItem" not in _actions(statement)
    assert granted == keys


@pytest.mark.parametrize(
    "role", ["ChatAttachmentSummarizerFunctionExecutionRole", "ChatAttachmentsResolverFunctionExecutionRole"]
)
def test_the_readers_can_decrypt_the_files(ai, role):
    policy = next(
        r
        for r in ai.values()
        if r["Type"] == "AWS::IAM::Policy"
        and {"Ref": role} in r["Properties"].get("Roles", [])
        and "s3." in json.dumps(r["Properties"]["PolicyDocument"])
    )
    assert "kms:Decrypt" in json.dumps(policy["Properties"]["PolicyDocument"])


def test_the_transcript_deletion_can_remove_every_version_of_the_files(ai):
    statements = _s3_statements(ai["TranscriptDeletionFunctionExecutionRole"])
    deletes = [s for s in statements if "s3:DeleteObjectVersion" in _actions(s)]
    assert any(f"{PREFIX}*" in r for s in deletes for r in _resources_of(s))
    listing = [s for s in statements if "s3:ListBucketVersions" in _actions(s)]
    prefixes = json.dumps([s["Condition"] for s in listing])
    assert f"{PREFIX}*" in prefixes


def test_the_call_event_processor_queues_the_summarizer(ai):
    rendered = json.dumps(ai, default=str)
    assert '"CHAT_ATTACHMENT_SUMMARIZER_ARN": {"Fn::GetAtt": "ChatAttachmentSummarizerFunction.Arn"}' in rendered
    assert '"Resource": {"Fn::GetAtt": "ChatAttachmentSummarizerFunction.Arn"}' in rendered


def test_the_summarizer_is_never_retried(ai):
    props = ai["ChatAttachmentSummarizerFunction"]["Properties"]
    assert props["EventInvokeConfig"]["MaximumRetryAttempts"] == 0


def test_the_virtual_participant_can_write_into_the_recordings_bucket():
    role = _resources(VP)["TaskRole"]
    writes = [s for s in _s3_statements(role) if "s3:PutObject" in _actions(s)]
    assert any("S3BucketName" in r and "/*" in r for s in writes for r in _resources_of(s))


def test_the_files_are_kept_no_longer_than_the_transcript():
    rules = _resources(MAIN)["RecordingsBucket"]["Properties"]["LifecycleConfiguration"]["Rules"]
    [rule] = [r for r in rules if r.get("Prefix") == PREFIX]
    assert rule["Status"] == "Enabled"
    assert rule["ExpirationInDays"] == {"Ref": "TranscriptionExpirationInDays"}
