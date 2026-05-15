# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
"""Resolve LMA stack outputs & nested-stack resources needed by the drivers.

Wraps the ``lma-sdk`` stack operations but adds discovery for resources that
aren't surfaced as top-level outputs — e.g. the Kinesis CallDataStream name,
the EventSourcing DynamoDB table name, the Cognito User Pool ID, the VP
scheduler Step Function ARN — which we must pull from the AI nested stack's
resources.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------
@dataclass
class LMAStackInfo:
    """Everything a driver could need to talk to a deployed LMA stack."""

    stack_name: str
    region: str

    # Endpoints (from root-stack Outputs)
    cloudfront_endpoint: str | None = None
    ws_endpoint: str | None = None
    appsync_graphql_url: str | None = None

    # Auth (from root-stack Outputs or nested-stack lookup)
    user_pool_id: str | None = None
    user_pool_client_id: str | None = None
    identity_pool_id: str | None = None

    # Pipeline resources (from AI nested stack)
    call_data_stream_name: str | None = None
    event_sourcing_table: str | None = None
    recordings_bucket: str | None = None
    uploads_pending_prefix: str = "lma-uploads-pending/"

    # Virtual Participant resources
    vp_scheduler_state_machine_arn: str | None = None
    vp_ecs_cluster_name: str | None = None
    vp_registry_table: str | None = None

    # Raw outputs (for power users / debugging)
    raw_outputs: dict[str, Any] | None = None

    def missing(self, keys: list[str]) -> list[str]:
        """Return the subset of ``keys`` that are still unresolved."""
        return [k for k in keys if not getattr(self, k, None)]


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------
# Root-stack Outputs we look for by key (case-sensitive). LMA has historically
# renamed a few of these, so we accept a list of aliases for each field.
_OUTPUT_ALIASES = {
    "cloudfront_endpoint": ["ApplicationCloudfrontEndpoint", "CloudFrontEndpoint"],
    "ws_endpoint": ["LMAWebsocketEndpoint", "WebsocketEndpoint", "WSEndpoint"],
    "appsync_graphql_url": [
        "GraphqlApiUrl",
        "AppSyncGraphqlUrl",
        "GraphQLApiURL",
    ],
    "user_pool_id": ["UserPoolId", "CognitoUserPoolId"],
    "user_pool_client_id": ["CognitoUserPoolClientId", "UserPoolClientId"],
    "identity_pool_id": ["IdentityPoolId", "CognitoIdentityPoolId"],
    "recordings_bucket": ["RecordingsS3Bucket", "CallAudioBucket", "RecordingsBucket"],
    "vp_scheduler_state_machine_arn": [
        "LMAVirtualParticipantSchedulerStateMachine",
        "VirtualParticipantSchedulerStateMachine",
    ],
    "vp_ecs_cluster_name": ["VirtualParticipantECSCluster", "VPECSCluster"],
    "vp_registry_table": ["VirtualParticipantTable", "VPRegistryTable"],
    # Direct output on LMA root stack (post v0.3).
    "call_data_stream_name": ["CallDataStreamName"],
}

# Physical-resource-id discovery when an output isn't exported.
# logical-id → which LMAStackInfo field it populates.
_NESTED_RESOURCE_DISCOVERY = {
    "CallDataStream": "call_data_stream_name",
    "EventSourcingTable": "event_sourcing_table",
}

# Map keys found in the ``LocalUITestingEnv`` output (used for `make ui-start`)
# back onto LMAStackInfo fields. LMA exposes this output as a space-separated
# list of ``KEY=VALUE`` pairs that captures everything the React UI needs to
# authenticate against the AppSync API.
_LOCAL_UI_ENV_MAP = {
    "VITE_USER_POOL_ID":          "user_pool_id",
    "VITE_USER_POOL_CLIENT_ID":   "user_pool_client_id",
    "VITE_IDENTITY_POOL_ID":      "identity_pool_id",
    "VITE_APPSYNC_GRAPHQL_URL":   "appsync_graphql_url",
}



def resolve(stack_name: str, region: str, profile: str | None = None) -> LMAStackInfo:
    """Load every resource a driver could need from CloudFormation.

    Raises ``RuntimeError`` if the root stack doesn't exist.
    """
    session_kwargs: dict[str, Any] = {"region_name": region}
    if profile:
        session_kwargs["profile_name"] = profile
    session = boto3.Session(**session_kwargs)
    cfn = session.client("cloudformation")

    try:
        resp = cfn.describe_stacks(StackName=stack_name)
    except ClientError as err:
        raise RuntimeError(
            f"Could not describe CloudFormation stack {stack_name!r}: {err}"
        ) from err
    stacks = resp.get("Stacks") or []
    if not stacks:
        raise RuntimeError(f"No such stack: {stack_name!r}")
    root = stacks[0]

    raw_outputs: dict[str, Any] = {o["OutputKey"]: o["OutputValue"] for o in root.get("Outputs", [])}
    info = LMAStackInfo(stack_name=stack_name, region=region, raw_outputs=raw_outputs)

    # 1. Try to populate everything from root outputs (aliases).
    for field_name, aliases in _OUTPUT_ALIASES.items():
        for alias in aliases:
            if alias in raw_outputs:
                setattr(info, field_name, raw_outputs[alias])
                break

    # 2. Fall back to the LocalUITestingEnv output — an LMA convention that
    # packages VITE_USER_POOL_ID / VITE_APPSYNC_GRAPHQL_URL / etc. into a
    # single space-separated KEY=VALUE output used by `make ui-start`. This
    # is the most reliable way to get AppSync + Cognito config when the
    # stack doesn't expose them as individual outputs.
    if "LocalUITestingEnv" in raw_outputs:
        _parse_local_ui_env(info, raw_outputs["LocalUITestingEnv"])

    # 3. For fields that weren't exported as outputs, walk the nested stack tree.
    missing_resources = [k for k in _NESTED_RESOURCE_DISCOVERY if not _field_for(info, k)]
    if missing_resources or not info.vp_ecs_cluster_name or not info.vp_registry_table:
        _populate_from_nested_stacks(info, cfn)

    return info


def _parse_local_ui_env(info: LMAStackInfo, value: str) -> None:
    """Parse the ``LocalUITestingEnv`` output (space-separated KEY=VALUE
    pairs) and populate the matching LMAStackInfo fields."""
    for pair in (value or "").split():
        if "=" not in pair:
            continue
        key, val = pair.split("=", 1)
        field_name = _LOCAL_UI_ENV_MAP.get(key)
        if field_name and not getattr(info, field_name, None):
            setattr(info, field_name, val.strip())



def _field_for(info: LMAStackInfo, logical_id: str) -> str | None:
    return getattr(info, _NESTED_RESOURCE_DISCOVERY.get(logical_id, ""), None)


def _populate_from_nested_stacks(info: LMAStackInfo, cfn) -> None:
    """Walk the root stack's resources, recurse into any AWS::CloudFormation::Stack
    children, and fill in physical IDs for resources we care about."""
    to_visit = [info.stack_name]
    visited: set[str] = set()
    while to_visit:
        current = to_visit.pop()
        if current in visited:
            continue
        visited.add(current)
        try:
            paginator = cfn.get_paginator("list_stack_resources")
            for page in paginator.paginate(StackName=current):
                for res in page.get("StackResourceSummaries", []):
                    logical = res.get("LogicalResourceId", "")
                    phys = res.get("PhysicalResourceId", "")
                    rtype = res.get("ResourceType", "")

                    if rtype == "AWS::CloudFormation::Stack" and phys:
                        # phys is the nested stack ARN — extract name
                        to_visit.append(phys.split("/")[-2] if "/" in phys else phys)
                        continue

                    if logical in _NESTED_RESOURCE_DISCOVERY:
                        field_name = _NESTED_RESOURCE_DISCOVERY[logical]
                        if not getattr(info, field_name, None):
                            setattr(info, field_name, phys)

                    # Best-effort VP registry / cluster detection.
                    if not info.vp_registry_table and logical.endswith(
                        "VirtualParticipantTable"
                    ):
                        info.vp_registry_table = phys
                    if (
                        not info.vp_ecs_cluster_name
                        and rtype == "AWS::ECS::Cluster"
                        and "VirtualParticipant" in logical
                    ):
                        info.vp_ecs_cluster_name = phys

                    # Recordings bucket fallback
                    if (
                        not info.recordings_bucket
                        and rtype == "AWS::S3::Bucket"
                        and "Recording" in logical
                    ):
                        info.recordings_bucket = phys
        except ClientError as err:
            logger.debug("list_stack_resources failed for %s: %s", current, err)
