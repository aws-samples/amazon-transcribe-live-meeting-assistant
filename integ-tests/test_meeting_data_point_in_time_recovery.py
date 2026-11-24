# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""The deployed tables' point-in-time recovery matches the stack parameter.

See docs/privacy-and-data-handling.md#copies-that-follow-their-own-lifetime.

Read-only, seconds, runs against any stack:

``test_content_tables_point_in_time_recovery_matches_the_parameter``
    Each table of the AI stack that holds meeting content (those the stack
    deploys: the voice enrollment table exists only with voice enrollment on)
    reports, from ``DescribeContinuousBackups``, point-in-time recovery off when
    ``MeetingDataPointInTimeRecoveryDays`` is 0, and on with that recovery period
    otherwise.
``test_configuration_tables_keep_a_35_day_window``
    The AI stack's configuration table keeps point-in-time recovery on for 35
    days, whatever the parameter says.

Both skip on a stack deployed before the parameter existed.
"""

from __future__ import annotations

import boto3
import pytest

PARAMETER = "MeetingDataPointInTimeRecoveryDays"
CONTENT_TABLES = (
    "EventSourcingTable",
    "VirtualParticipantTable",
    "TroubleshootingJobsTable",
    "VoiceEnrollmentTable",
)
CONFIGURATION_TABLES = ("ConfigurationTable",)


def _stack_resources(cfn, stack: str) -> list[dict]:
    resources: list[dict] = []
    for page in cfn.get_paginator("list_stack_resources").paginate(StackName=stack):
        resources.extend(page["StackResourceSummaries"])
    return resources


@pytest.fixture(scope="module")
def deployment(stack_name: str, region: str | None) -> dict:
    session = boto3.Session(region_name=region)
    cfn = session.client("cloudformation")
    stack = cfn.describe_stacks(StackName=stack_name)["Stacks"][0]
    parameters = {p["ParameterKey"]: p.get("ParameterValue") for p in stack.get("Parameters", [])}
    if PARAMETER not in parameters:
        pytest.skip(f"{stack_name} was deployed before {PARAMETER} existed")
    ai_stack = next(
        (
            r["PhysicalResourceId"]
            for r in _stack_resources(cfn, stack_name)
            if r["LogicalResourceId"] == "AISTACK"
        ),
        None,
    )
    assert ai_stack, f"{stack_name} has no AISTACK nested stack"
    tables = {
        r["LogicalResourceId"]: r["PhysicalResourceId"]
        for r in _stack_resources(cfn, ai_stack)
        if r["ResourceType"] == "AWS::DynamoDB::Table"
    }
    return {
        "days": int(parameters[PARAMETER]),
        "tables": tables,
        "dynamodb": session.client("dynamodb"),
    }


def _point_in_time_recovery(dynamodb, table: str) -> dict:
    backups = dynamodb.describe_continuous_backups(TableName=table)["ContinuousBackupsDescription"]
    return backups["PointInTimeRecoveryDescription"]


def test_content_tables_point_in_time_recovery_matches_the_parameter(deployment: dict) -> None:
    days = deployment["days"]
    present = [t for t in CONTENT_TABLES if t in deployment["tables"]]
    assert "EventSourcingTable" in present, "the AI stack has no meetings table"
    for logical_id in present:
        pitr = _point_in_time_recovery(deployment["dynamodb"], deployment["tables"][logical_id])
        if days == 0:
            assert pitr["PointInTimeRecoveryStatus"] == "DISABLED", f"{logical_id}: {pitr}"
        else:
            assert pitr["PointInTimeRecoveryStatus"] == "ENABLED", f"{logical_id}: {pitr}"
            assert pitr.get("RecoveryPeriodInDays") == days, f"{logical_id}: {pitr}"


def test_configuration_tables_keep_a_35_day_window(deployment: dict) -> None:
    for logical_id in CONFIGURATION_TABLES:
        assert logical_id in deployment["tables"], f"the AI stack has no {logical_id}"
        pitr = _point_in_time_recovery(deployment["dynamodb"], deployment["tables"][logical_id])
        assert pitr["PointInTimeRecoveryStatus"] == "ENABLED", f"{logical_id}: {pitr}"
        assert pitr.get("RecoveryPeriodInDays", 35) == 35, f"{logical_id}: {pitr}"
