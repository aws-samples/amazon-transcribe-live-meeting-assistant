#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""Find LMA's CloudWatch log groups, and say which component owns each one.

WHY DISCOVERY AND NOT CONSTRUCTION
----------------------------------
An LMA deployment's log group names cannot be derived from the stack name the
operator typed. Four independent reasons, all of them live in the current tree:

* Most groups are named ``/${AWS::StackName}/lambda/<LogicalName>`` where
  ``AWS::StackName`` is the **nested** stack's generated name
  (``LMA-Bob-AISTACK-1A2B3C``), not the name the operator chose.
* A handful use ``${LMAStackName}`` — the parent's name — instead, so two
  groups for the same component can sit under different prefixes. The virtual
  participant manager has one of each.
* The MicroVM groups live outside the stack-name namespace altogether, under
  ``/aws/lambda-microvms/``, and their suffixes were ``-vp`` and ``-asr``
  before they became ``-vp-logs`` and ``-asr-logs``. Any agent carrying the
  older spelling would have reported "no logs" on a current deployment and
  been believed.
* AppSync's group is created by the service, named after the API id rather
  than the stack.

So the only reliable name is one CloudWatch has just told us about. The
prefix-pagination approach below is the one already used by
``lma_sdk._core.stack.LMAStack.get_log_groups``; this adds the two
``/aws/``-rooted namespaces that one misses, the AppSync group, and the
component labelling the agent needs in order to pick a group to query.

ON ``stored_bytes``
-------------------
Every group is reported with the bytes CloudWatch holds for it, because an
empty group is the single most misleading piece of evidence in this solution.
Under the default ``VPLaunchType=MICROVM`` the Fargate group
``/<aistack>/fargate/VirtualParticipant`` exists and is permanently empty, so
an agent that queries it finds nothing and concludes the virtual participant
never ran. Handing it ``stored_bytes: 0`` up front turns that into "this
deployment does not use this group" instead.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence
from urllib.parse import urlparse

# ── Component taxonomy ──────────────────────────────────────────────────────
#
# Ordered: the first pattern that matches wins, so the specific entries have
# to precede the catch-alls. Patterns are matched against the full log group
# name, case-insensitively.

VIRTUAL_PARTICIPANT = "virtual-participant"
VP_LAUNCH_STATE_MACHINE = "vp-launch-state-machine"
WEBSOCKET_TRANSCRIBER = "websocket-transcriber"
ASR_MICROVM = "asr-microvm"
TRANSCRIPT_PIPELINE = "transcript-pipeline"
MEETING_ASSISTANT = "meeting-assistant"
GRAPHQL_API = "graphql-api"
AUTH = "auth"
VOICE_ENROLLMENT = "voice-enrollment"
DEPLOYMENT = "deployment"
OTHER = "other"

_COMPONENT_PATTERNS: Sequence[tuple[str, str]] = (
    # The VP container's own stdout. Under MICROVM this is the only place the
    # join attempt is recorded at all.
    (r"/aws/lambda-microvms/.*-vp-logs$", VIRTUAL_PARTICIPANT),
    (r"/aws/lambda-microvms/.*-asr-logs$", ASR_MICROVM),
    # EXPRESS state machine: its vended log group is the ONLY record that a
    # launch attempt happened, because an EXPRESS execution has no
    # execution-history API to query afterwards.
    (r"/aws/vendedlogs/states/", VP_LAUNCH_STATE_MACHINE),
    (r"/aws/appsync/apis/", GRAPHQL_API),
    (r"/fargate/VirtualParticipant$", VIRTUAL_PARTICIPANT),
    (r"/fargate/TranscriberWebsocket$", WEBSOCKET_TRANSCRIBER),
    (r"/lambda/Asr(Launcher|ImageSource|MicrovmImagePruner)", ASR_MICROVM),
    (
        r"/lambda/(VirtualParticipantManager|VPScheduler|VPTaskReaper"
        r"|VPMicrovmLauncherFunction|VPMicrovmImagePruner|MicrovmVncTokenFunction"
        r"|VncEdgeTokenFunction|ZoomCredentialsManager|ASGDeletionHandlerFunction)",
        VIRTUAL_PARTICIPANT,
    ),
    (
        r"/lambda/(CallEventProcessorFunction|StaleMeetingReaperFunction"
        r"|SpeakerNameResolverFunction|FetchTranscript|ListCallsGsiResolver"
        r"|MeetingControlsResolverFunction|PCAIntegrationFunction"
        r"|UploadMeeting\w+|MeetingInvitationParserFunction)",
        TRANSCRIPT_PIPELINE,
    ),
    (
        r"/lambda/(Strands\w+|BedrockSummaryLambda|Async\w+Orchestrator"
        r"|QueryKnowledgeBaseResolverFunction|SummaryRegeneratorFunction"
        r"|MCPServer\w*|MCPApiKey\w*|StartIngestionJobFunction)",
        MEETING_ASSISTANT,
    ),
    (r"/lambda/VoiceEnrollment", VOICE_ENROLLMENT),
    (
        r"/lambda/(Cognito\w+|UserManagement|OAuthManager)",
        AUTH,
    ),
    (
        r"/lambda/(\w*CustomResourceFunction|\w*UploadFunction|\w*CodeBuild\w*"
        r"|\w*Cleanup\w*|\w*Delete\w*|SetupFunction|GetDomainLambda"
        r"|UploadManifestLambda|Backfill\w+|KMSKeyMonitoringFunction"
        r"|Update\w+ResolverFunction|GetCloudFrontPrefixListFunction"
        r"|GetEventApiDnsFunction|GetS3VectorBucketNameFunction)",
        DEPLOYMENT,
    ),
)

# What an agent should expect to find in each component's logs. Shipped with
# the discovery result so a tool call answers "which group do I query" without
# a second round trip to the model.
COMPONENT_NOTES: Dict[str, str] = {
    VIRTUAL_PARTICIPANT: (
        "Meeting-join and in-meeting behaviour of the virtual participant. The "
        "structured exit code is here and nowhere else: grep 'VP exit:' for a "
        "line carrying reason= and trigger=. The AI vision classifier's "
        "per-30-second verdict (JoinState) is also logs-only."
    ),
    VP_LAUNCH_STATE_MACHINE: (
        "Whether a launch was attempted at all, and how it ended: "
        "MarkVPFailed, CheckRunTaskFailures, MarkWaitingForCapacity. The state "
        "machine is EXPRESS, so this log group is the only record — there is no "
        "execution history to describe."
    ),
    WEBSOCKET_TRANSCRIBER: (
        "Audio ingestion and the speech-to-text session. Lines are "
        "bracket-tagged: [AUTH] for the WebSocket upgrade, [TRANSCRIBING] for "
        "the Amazon Transcribe session lifecycle, [ASR] for the MicroVM "
        "engine, [DIARIZATION] for speaker labelling."
    ),
    ASR_MICROVM: (
        "The on-demand ASR server, when EnableMicrovmAsr is on and a source "
        "has been moved onto it. Absent or empty otherwise."
    ),
    TRANSCRIPT_PIPELINE: (
        "Kinesis -> DynamoDB -> AppSync. 'reporting Kinesis batch item "
        "failures' means records will be redelivered; 'skipped Kinesis records "
        "that could not be decoded' means they were discarded to the "
        "discarded-records queue and will not be."
    ),
    MEETING_ASSISTANT: (
        "Bedrock calls, Strands tool use, MCP servers, summarization. Bedrock "
        "throttling and guardrail interventions surface here."
    ),
    GRAPHQL_API: (
        "AppSync request and resolver logs. Useful for authorization failures "
        "and for confirming whether a mutation the UI sent ever arrived."
    ),
    AUTH: "Sign-up, sign-in, email-domain checks and OAuth flows.",
    VOICE_ENROLLMENT: "Voiceprint enrollment, consent and retention.",
    DEPLOYMENT: (
        "Custom resources and config seeders. These run at deploy time; a "
        "failure here usually means a stack event rather than a meeting "
        "symptom."
    ),
    OTHER: "Not classified. Read the name to judge whether it is relevant.",
}


def classify_log_group(name: str) -> str:
    """Return the component that owns `name`."""
    for pattern, component in _COMPONENT_PATTERNS:
        if re.search(pattern, name, re.IGNORECASE):
            return component
    return OTHER


@dataclass(frozen=True)
class LogGroupInfo:
    """One discovered log group."""

    name: str
    component: str
    stored_bytes: int
    retention_days: Optional[int]

    @property
    def has_data(self) -> bool:
        """False for a group that exists but has never been written to.

        The distinction the agent needs: a group with no data is not evidence
        of absence, it is absence of evidence, and usually means this
        deployment does not use that code path.
        """
        return self.stored_bytes > 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "logGroupName": self.name,
            "component": self.component,
            "storedBytes": self.stored_bytes,
            "hasData": self.has_data,
            "retentionDays": self.retention_days,
        }


def appsync_log_group_candidate(graphql_url: str) -> Optional[str]:
    """Derive the AppSync log group name from the GraphQL endpoint URL.

    AppSync's log group is service-managed and named ``/aws/appsync/apis/
    <apiId>``, which is not derivable from the stack name — but the API id is
    the first label of the endpoint host, and the function already has that
    endpoint in its environment for streaming.

    This returns a *candidate*. The caller confirms it against
    ``describe_log_groups`` before reporting it, so a deployment with AppSync
    logging switched off does not get a group name that cannot be queried.
    Deriving a candidate and then verifying it is not the same thing as
    constructing a name and trusting it.
    """
    if not graphql_url:
        return None
    host = urlparse(graphql_url).hostname or ""
    label = host.split(".")[0]
    # AppSync api ids are 26 lowercase base32 characters. Anything else means
    # the URL is not an AppSync endpoint and there is nothing to derive.
    if not re.fullmatch(r"[a-z0-9]{20,32}", label):
        return None
    return f"/aws/appsync/apis/{label}"


def stack_log_group_prefixes(stack_name: str) -> List[str]:
    """The prefixes under which an LMA deployment's log groups can appear.

    ``/<stack>-`` catches every nested stack's groups, whose names begin with
    the generated nested stack name; ``/<stack>/`` catches the groups named
    after the parent stack directly. The two ``/aws/``-rooted prefixes are the
    namespaces CloudWatch, not CloudFormation, controls.
    """
    if not stack_name:
        return []
    return [
        f"/{stack_name}-",
        f"/{stack_name}/",
        f"/aws/lambda-microvms/{stack_name}-",
        f"/aws/vendedlogs/states/{stack_name}-",
    ]


def discover_log_groups(
    logs_client: Any,
    stack_name: str,
    *,
    max_log_groups: int,
    graphql_url: str = "",
    components: Optional[Iterable[str]] = None,
) -> List[LogGroupInfo]:
    """Page `describe_log_groups` over every namespace LMA writes to.

    Args:
        logs_client: A CloudWatch Logs client.
        stack_name: The LMA parent stack name.
        max_log_groups: Hard cap on the number returned.
        graphql_url: AppSync endpoint, used to look for the service-managed
            AppSync group. Optional.
        components: Restrict the result to these components. A name that is
            not a known component matches nothing, which is deliberate — the
            agent gets an empty result it can see rather than a silently
            unfiltered one.

    Returns:
        Groups sorted by component then name, so related groups are adjacent
        in the model's context.
    """
    wanted = set(components) if components is not None else None
    found: Dict[str, LogGroupInfo] = {}

    prefixes = stack_log_group_prefixes(stack_name)
    candidate = appsync_log_group_candidate(graphql_url)
    if candidate:
        # A full group name is also a valid prefix, which is how the candidate
        # gets verified rather than assumed.
        prefixes.append(candidate)

    paginator = logs_client.get_paginator("describe_log_groups")
    for prefix in prefixes:
        for page in paginator.paginate(logGroupNamePrefix=prefix):
            for group in page.get("logGroups", []):
                name = group.get("logGroupName")
                if not name or name in found:
                    continue
                component = classify_log_group(name)
                if wanted is not None and component not in wanted:
                    continue
                found[name] = LogGroupInfo(
                    name=name,
                    component=component,
                    stored_bytes=int(group.get("storedBytes") or 0),
                    retention_days=group.get("retentionInDays"),
                )
                if len(found) >= max_log_groups:
                    # Stop paginating rather than collecting and slicing: the
                    # cap exists to bound the API calls as well as the result.
                    return _sorted(found.values())
    return _sorted(found.values())


def _sorted(groups: Iterable[LogGroupInfo]) -> List[LogGroupInfo]:
    return sorted(groups, key=lambda g: (g.component, g.name))
