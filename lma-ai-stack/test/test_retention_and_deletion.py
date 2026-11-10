# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""How long the recordings bucket and the Virtual Participant table keep data,
and what deleting a meeting is allowed to remove. See
docs/privacy-and-data-handling.md.

Checked statically (no AWS):

- the recordings bucket expires noncurrent object versions after one day and
  removes expired delete markers, for every object in the bucket;
- the meeting controls Lambda can list and delete object versions, which
  deleting a meeting does for each of its files;
- the Virtual Participant table has a time to live on ExpiresAfter, and
  createVirtualParticipant writes ExpiresAfter from
  MeetingRecordExpirationInDays, counted from the later of now and the
  scheduled time;
- the summary Lambda is told whether the transcript knowledge base exists.

The expiry the create resolver writes is also evaluated by AppSync itself when
LMA_EVALUATE_APPSYNC=1 is set (see test_shared_with_access_evaluated.py).
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
MAIN = REPO_ROOT / "lma-main.yaml"
AI_STACK = REPO_ROOT / "lma-ai-stack" / "deployment" / "lma-ai-stack.yaml"


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


def _create_vp_request(ai_stack: dict) -> str:
    request = ai_stack["CreateVirtualParticipantResolver"]["Properties"]["RequestMappingTemplate"]
    assert isinstance(request, dict) and "Fn::Sub" in request
    return request["Fn::Sub"]


# ── recordings bucket ───────────────────────────────────────────────────────


def _recordings_rules(main: dict) -> dict:
    rules = main["RecordingsBucket"]["Properties"]["LifecycleConfiguration"]["Rules"]
    return {rule["Id"]: rule for rule in rules}


def test_the_recordings_bucket_is_versioned(main: dict) -> None:
    props = main["RecordingsBucket"]["Properties"]
    assert props["VersioningConfiguration"]["Status"] == "Enabled"


def test_noncurrent_versions_of_every_recordings_object_expire_after_one_day(main: dict) -> None:
    rule = _recordings_rules(main)["ExpireNoncurrentVersions"]
    assert rule["Status"] == "Enabled"
    assert rule["NoncurrentVersionExpiration"] == {"NoncurrentDays": 1}
    # The whole bucket: no prefix, filter or tag narrows the rule.
    for narrowing in ("Prefix", "TagFilters", "ObjectSizeGreaterThan", "ObjectSizeLessThan"):
        assert narrowing not in rule, narrowing


def test_expired_delete_markers_in_the_recordings_bucket_are_removed(main: dict) -> None:
    rule = _recordings_rules(main)["ExpireNoncurrentVersions"]
    assert rule["ExpiredObjectDeleteMarker"] is True
    # S3 refuses ExpiredObjectDeleteMarker alongside an expiry of current objects.
    assert "ExpirationInDays" not in rule and "ExpirationDate" not in rule


def test_the_content_retention_rules_are_unchanged(main: dict) -> None:
    rules = _recordings_rules(main)
    expected = {
        "AudioRetention": {"Ref": "AudioRecordingExpirationInDays"},
        "VideoRetention": {"Ref": "AudioRecordingExpirationInDays"},
        "TranscriptRetention": {"Ref": "TranscriptionExpirationInDays"},
        "VideoKeyframeRetention": {"Ref": "TranscriptionExpirationInDays"},
        "ConsentEvidenceRetention": {"Ref": "TranscriptionExpirationInDays"},
        "VideoChunkCleanup": 1,
        "PendingUploadCleanup": 7,
    }
    for rule_id, days in expected.items():
        assert rules[rule_id]["ExpirationInDays"] == days, rule_id


def test_meeting_deletion_may_list_and_delete_object_versions(ai_stack: dict) -> None:
    role = ai_stack["MeetingControlsResolverFunctionExecutionRole"]["Properties"]
    actions = {
        action
        for policy in role["Policies"]
        for statement in policy["PolicyDocument"]["Statement"]
        for action in statement["Action"]
        if isinstance(action, str) and action.startswith("s3:")
    }
    assert {"s3:ListBucketVersions", "s3:DeleteObjectVersion"} <= actions


# ── Virtual Participant records ─────────────────────────────────────────────


def test_the_virtual_participant_table_expires_rows_on_expires_after(ai_stack: dict) -> None:
    props = ai_stack["VirtualParticipantTable"]["Properties"]
    assert props["TimeToLiveSpecification"] == {"AttributeName": "ExpiresAfter", "Enabled": True}


def test_a_new_virtual_participant_row_is_given_the_meeting_records_retention(
    ai_stack: dict,
) -> None:
    vtl = _create_vp_request(ai_stack)
    assert "${MeetingRecordExpirationInDays} * 86400" in vtl
    assert '$util.qr($input.put("ExpiresAfter", $expiresAfter))' in vtl
    # Counted from the scheduled time when that is later than now.
    assert "$ctx.args.input.meetingTime > $expiresFrom" in vtl
    # Written before the PutItem that stores $input.
    assert vtl.index('"ExpiresAfter"') < vtl.index('"operation": "PutItem"')
    # The only substitution is the parameter: every VTL reference is unbraced.
    assert re.findall(r"\$\{([^}]*)\}", vtl) == ["MeetingRecordExpirationInDays"]


@pytest.mark.real_aws
@pytest.mark.skipif(
    os.environ.get("LMA_EVALUATE_APPSYNC") != "1",
    reason="evaluates the template with the AppSync API; set LMA_EVALUATE_APPSYNC=1 to run",
)
@pytest.mark.parametrize("days", ["90", "0.5"])
def test_appsync_evaluates_the_row_expiry(ai_stack: dict, days: str) -> None:
    import boto3

    template = _create_vp_request(ai_stack).replace("${MeetingRecordExpirationInDays}", days)
    client = boto3.client("appsync")
    retention = int(float(days) * 86400)
    scheduled = int(time.time()) + 400 * 86400
    for args, expires_from in (
        ({"meetingName": "m", "meetingPlatform": "ZOOM", "meetingId": "1", "status": "X"}, None),
        (
            {
                "meetingName": "m",
                "meetingPlatform": "ZOOM",
                "meetingId": "1",
                "status": "SCHEDULED",
                "meetingTime": scheduled,
            },
            scheduled,
        ),
    ):
        context = {
            "arguments": {"input": args},
            "identity": {"username": "owner@example.com", "sub": "s"},
        }
        before = int(time.time())
        result = client.evaluate_mapping_template(template=template, context=json.dumps(context))
        assert "error" not in result, result.get("error")
        stored = json.loads(result["evaluationResult"])["attributeValues"]["ExpiresAfter"]
        expires = int(stored["N"])
        if expires_from is None:
            assert before + retention <= expires <= int(time.time()) + retention + 5
        else:
            assert expires == expires_from + retention


# ── the summary's knowledge-base copy ───────────────────────────────────────


def test_the_summary_lambda_is_told_whether_there_is_a_transcript_knowledge_base(
    ai_stack: dict,
) -> None:
    variables = ai_stack["BedrockSummaryLambda"]["Properties"]["Environment"]["Variables"]
    assert variables["TRANSCRIPT_KB_ID"] == {"Ref": "BedrockKnowledgeBaseID"}
