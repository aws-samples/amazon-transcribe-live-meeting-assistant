# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""The meeting archive's resources: the function that builds a meeting's ZIP
archive, its role, its AppSync wiring, and the lifecycle rule that removes the
archives a day after they are written.

Asserted here: the archives' prefix expires after one day, with unfinished
multipart uploads; the function's role writes only under that prefix, reads
only the meeting content prefixes an archive holds, lists only the consent
evidence prefix, reads only the meeting's own DynamoDB partitions, and invokes
only itself; the role can use the bucket's key; both AppSync fields resolve
through the function.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
MAIN = REPO_ROOT / "lma-main.yaml"
AI_STACK = REPO_ROOT / "lma-ai-stack" / "deployment" / "lma-ai-stack.yaml"
ARCHIVE_PREFIX = "lma-meeting-archives/"


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


def _resources(path: Path) -> dict:
    return yaml.load(path.read_text(encoding="utf-8"), Loader=_CfnLoader)["Resources"]


@pytest.fixture(scope="module")
def main() -> dict:
    return _resources(MAIN)


@pytest.fixture(scope="module")
def ai_stack() -> dict:
    return _resources(AI_STACK)


def _statements(ai_stack: dict) -> list:
    role = ai_stack["MeetingArchiveFunctionExecutionRole"]["Properties"]
    return [
        statement
        for policy in role["Policies"]
        for statement in policy["PolicyDocument"]["Statement"]
    ]


def _resources_of(statement: dict) -> list:
    resource = statement["Resource"]
    resources = resource if isinstance(resource, list) else [resource]
    return [r.get("Fn::Sub", r) if isinstance(r, dict) else r for r in resources]


def _for_action(ai_stack: dict, action: str) -> list:
    return [s for s in _statements(ai_stack) if action in s["Action"]]


def test_archives_expire_a_day_after_they_are_written(main: dict) -> None:
    rules = {
        rule["Id"]: rule
        for rule in main["RecordingsBucket"]["Properties"]["LifecycleConfiguration"]["Rules"]
    }
    rule = rules["MeetingArchiveCleanup"]
    assert rule["Status"] == "Enabled"
    assert rule["Prefix"] == ARCHIVE_PREFIX
    assert rule["ExpirationInDays"] == 1
    assert rule["AbortIncompleteMultipartUpload"] == {"DaysAfterInitiation": 1}


def test_the_function_writes_only_under_the_archive_prefix(ai_stack: dict) -> None:
    for action in ("s3:PutObject", "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts"):
        statements = _for_action(ai_stack, action)
        assert statements, action
        for statement in statements:
            assert _resources_of(statement) == [
                f"arn:${{AWS::Partition}}:s3:::${{S3BucketName}}/{ARCHIVE_PREFIX}*"
            ]
    writes = {
        a
        for s in _statements(ai_stack)
        for a in s["Action"]
        if a.startswith("s3:") and "Delete" in a
    }
    assert not writes, "the function deletes nothing"


def test_the_function_reads_only_the_content_an_archive_holds(ai_stack: dict) -> None:
    allowed = {
        f"arn:${{AWS::Partition}}:s3:::${{S3BucketName}}/{prefix}*"
        for prefix in (
            "lma-audio-recordings/",
            "lma-video-recordings/",
            "lma-video-keyframes/",
            "lma-chat-attachments/",
            "lma-consent-evidence/",
            ARCHIVE_PREFIX,
        )
    }
    read = {r for s in _for_action(ai_stack, "s3:GetObject") for r in _resources_of(s)}
    assert read == allowed
    listing = _for_action(ai_stack, "s3:ListBucket")
    assert len(listing) == 1
    assert listing[0]["Condition"] == {"StringLike": {"s3:prefix": ["lma-consent-evidence/*"]}}


def test_the_function_reads_only_meeting_partitions_and_writes_no_items(ai_stack: dict) -> None:
    dynamo = [
        s for s in _statements(ai_stack) if any(a.startswith("dynamodb:") for a in s["Action"])
    ]
    assert len(dynamo) == 1
    assert sorted(dynamo[0]["Action"]) == ["dynamodb:GetItem", "dynamodb:Query"]
    keys = dynamo[0]["Condition"]["ForAllValues:StringLike"]["dynamodb:LeadingKeys"]
    assert sorted(keys) == ["att#*", "c#*", "kf#*", "trs#*"]


def test_the_function_invokes_only_itself(ai_stack: dict) -> None:
    invoke = _for_action(ai_stack, "lambda:InvokeFunction")
    assert len(invoke) == 1
    assert _resources_of(invoke[0]) == [
        "arn:${AWS::Partition}:lambda:${AWS::Region}:${AWS::AccountId}:function:${AWS::StackName}-MeetingArchive"
    ]
    assert ai_stack["MeetingArchiveFunction"]["Properties"]["FunctionName"] == {
        "Fn::Sub": "${AWS::StackName}-MeetingArchive"
    }


def test_the_function_has_a_boundary_its_own_log_group_and_no_retries(ai_stack: dict) -> None:
    function = ai_stack["MeetingArchiveFunction"]
    props = function["Properties"]
    assert props["Timeout"] == 900
    assert props["EventInvokeConfig"]["MaximumRetryAttempts"] == 0
    assert function["DependsOn"] == ["MeetingArchiveFunctionLogGroup"]
    assert (
        props["LoggingConfig"]["LogGroup"]
        == ai_stack["MeetingArchiveFunctionLogGroup"]["Properties"]["LogGroupName"]
    )
    assert "PermissionsBoundary" in ai_stack["MeetingArchiveFunctionExecutionRole"]["Properties"]


def test_the_role_may_use_the_recordings_bucket_key(ai_stack: dict) -> None:
    roles = ai_stack["RecordingsBucketEncryptionKeyPolicy"]["Properties"]["Roles"]
    assert {"Ref": "MeetingArchiveFunctionExecutionRole"} in roles


@pytest.mark.parametrize(
    ("resolver", "type_name", "field"),
    [
        ("PrepareMeetingArchiveAppSyncResolver", "Mutation", "prepareMeetingArchive"),
        ("GetMeetingArchiveAppSyncResolver", "Query", "getMeetingArchive"),
    ],
)
def test_both_fields_resolve_through_the_archive_function(
    ai_stack: dict, resolver, type_name, field
) -> None:
    props = ai_stack[resolver]["Properties"]
    assert (props["TypeName"], props["FieldName"]) == (type_name, field)
    assert props["DataSourceName"] == {"Fn::GetAtt": "MeetingArchiveLambdaDataSource.Name"}
    source = ai_stack["MeetingArchiveLambdaDataSource"]["Properties"]
    assert source["LambdaConfig"]["LambdaFunctionArn"] == {
        "Fn::GetAtt": "MeetingArchiveFunction.Arn"
    }
