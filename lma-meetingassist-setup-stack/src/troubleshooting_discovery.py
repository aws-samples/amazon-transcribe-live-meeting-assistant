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
  ``/aws/lambda-microvms/``, and come in two generations. The stack declares
  ``<stack>-vp-logs`` and ``<stack>-asr-logs``; the image build logs there,
  and so does every MicroVM whose RunMicrovm call named that group, which the
  launchers do from this release on. Before that, a running MicroVM logged to
  ``<stack>-vp`` or ``<stack>-asr``, which the service created after the
  image's name and the stack does not own. Where a MicroVM's output went is
  fixed when it is launched, not when its image was built: an upgraded stack
  keeps both groups, and a MicroVM launched before the deployment was updated
  to v0.3.11 — including one launched while the update was in progress —
  wrote to the older one. Either name searched alone reports
  "no logs" for some participants, and would be believed. See
  ``MICROVM_LOG_GENERATIONS``.
* AppSync's group is created by the service, named after the API id rather
  than the stack.

So the only reliable name is one CloudWatch has just told us about. The
prefix-pagination approach below is the one already used by
``lma_sdk._core.stack.LMAStack.get_log_groups``; this adds the two
``/aws/``-rooted namespaces that one misses, the AppSync group, and the
component labelling the agent needs in order to pick a group to query.

ON ``stored_bytes``
-------------------
Every group is reported with the bytes CloudWatch currently stores for it,
because an empty group is the single most misleading piece of evidence in this
solution. Under the default ``VPLaunchType=MICROVM`` the Fargate group
``/<vpstack>/fargate/VirtualParticipant`` — declared in the Virtual
Participant stack — exists and is permanently empty, so an agent that queries
it finds nothing and could conclude the virtual participant never ran.

**But the figure is weaker than it looks, and every reader of it says so.** It
counts what is stored *now*, so a group in active use also reports zero once
everything in it has aged past the retention period. The signal is therefore a
hint about where to look first, never grounds for skipping a group: a
participant idle for a fortnight and failing today reports zero on the very
group holding the failure. Whether a path is genuinely unused is a question
about configuration, which ``get_stack_parameters`` answers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence

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
    # join attempt is recorded at all. Both generations of name land on the
    # same component, so filtering by component returns both.
    (r"/aws/lambda-microvms/.*-vp(-logs)?$", VIRTUAL_PARTICIPANT),
    (r"/aws/lambda-microvms/.*-asr(-logs)?$", ASR_MICROVM),
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
        # TroubleshootingAgentFunction is this agent's own group. Classified
        # rather than left as "other" so "why did the last diagnosis fail" is
        # answerable by component like anything else.
        r"/lambda/(Strands\w+|TroubleshootingAgentFunction"
        r"|BedrockSummaryLambda|Async\w+Orchestrator"
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
        "line carrying reason= and trigger=. That line is written only when "
        "the participant leaves on its own; one stopped from outside writes "
        "'Received SIGTERM' and, on a MicroVM, '[terminate] signalling app to "
        "exit' instead. The AI vision classifier's per-30-second verdict "
        "(JoinState) is also logs-only."
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


# ── MicroVM log group generations ───────────────────────────────────────────

CURRENT_GENERATION = "current"
LEGACY_GENERATION = "legacy"

#: (suffix, generation) for each MicroVM log group name a deployment can hold,
#: appended to ``/aws/lambda-microvms/<stack>``. Discovery looks each one up and
#: reports only those CloudWatch returns: a deployment created at v0.3.11 or
#: later never had the legacy groups, and one upgraded from earlier has both.
MICROVM_LOG_GENERATIONS: Sequence[tuple[str, str]] = (
    ("-vp-logs", CURRENT_GENERATION),
    ("-asr-logs", CURRENT_GENERATION),
    ("-vp", LEGACY_GENERATION),
    ("-asr", LEGACY_GENERATION),
)

# Stream names, as read from a deployed stack's groups; the service documents
# neither form. A running MicroVM's stream is named
# "<YYYY/MM/DD>[<image version>]<microvmId>" — e.g.
# "2026/10/24[55.0]microvm-6ac75d14-…", the same "microvm-…" id the launch
# state machine logs. An image build's streams are named
# "<image version>/<uuid>" and carry no MicroVM id. The runtime form was
# observed in the service-created (legacy) group; that the service names
# runtime streams the same way in the declared group is expected but was not
# observed when this was written, which is why the current note below tells
# the agent to fall back to message text rather than trust the stream name.
# (For the legacy group the same fallback is in caveats(), which is emitted
# whenever a legacy group is listed.)
_GENERATION_NOTES: Dict[str, str] = {
    CURRENT_GENERATION: (
        "Current MicroVM log group, declared by the stack. Holds the image "
        "build output and the output of every MicroVM launched since the "
        "deployment was updated to this release. A MicroVM launched before "
        "that update finished logged to the legacy group instead, if one is "
        "listed. A running MicroVM's stream name should contain the MicroVM "
        "id the launch state machine logs as microvmId; build streams do not. "
        "That naming is observed rather than documented, so if no stream "
        "matches, search the group's message text for the participant's id "
        "before treating the miss as an absence."
    ),
    LEGACY_GENERATION: (
        "Legacy MicroVM log group, created by the service rather than the "
        "stack, and kept with its contents when the stack was upgraded. A "
        "participant whose MicroVM was launched before the deployment was "
        "updated to this release logged here and not to the current group — "
        "including one launched while the update was in progress. Its stream "
        "names contain the MicroVM id the launch state machine logs as "
        "microvmId."
    ),
}


def microvm_log_group_names(stack_name: str) -> Dict[str, str]:
    """Every MicroVM log group name this deployment can hold, to its generation.

    These are names to look up, not names to trust: discovery verifies each
    one against ``describe_log_groups`` and reports only those that exist.
    """
    if not stack_name:
        return {}
    return {
        f"/aws/lambda-microvms/{stack_name}{suffix}": generation
        for suffix, generation in MICROVM_LOG_GENERATIONS
    }


@dataclass(frozen=True)
class LogGroupInfo:
    """One discovered log group."""

    name: str
    component: str
    stored_bytes: int
    retention_days: Optional[int]
    #: CURRENT_GENERATION or LEGACY_GENERATION for a MicroVM group; None for
    #: every other group, which has only ever had one name.
    generation: Optional[str] = None

    @property
    def has_data(self) -> bool:
        """Whether CloudWatch reports stored bytes for this group.

        **Weaker than "has ever been written to", and the difference matters.**
        ``storedBytes`` counts what is stored *now*, so a group reports zero
        when every event in it has aged past its retention — and also, briefly,
        when its first events are very recent, because the figure is not
        updated in real time.

        So a zero here is a hint that a path may be unused, not proof that it
        is. A virtual participant idle for a fortnight and failing today would
        report zero on the group holding the failure. The guidance built on
        this says so, and does not tell the agent to skip such a group.
        """
        return self.stored_bytes > 0

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "logGroupName": self.name,
            "component": self.component,
            "storedBytes": self.stored_bytes,
            "hasData": self.has_data,
            "retentionDays": self.retention_days,
        }
        # Only on the groups it applies to: the label is read by the model, and
        # a null on every other group is noise it would have to discount.
        if self.generation:
            out["microvmLogGeneration"] = self.generation
            out["note"] = _GENERATION_NOTES[self.generation]
        return out


def appsync_log_group_candidate(api_id: str) -> Optional[str]:
    """The AppSync log group name for `api_id`, as a candidate to verify.

    AppSync's log group is service-managed and named
    ``/aws/appsync/apis/<apiId>``, which is not derivable from the stack name.
    The id therefore has to come from the deployment: it is passed to this
    function as an environment variable, set from the ``AppSyncApiId``
    CloudFormation parameter — the same value the IAM grant for this group is
    scoped to, so the two cannot disagree.

    Taking it from the stack rather than reading it out of the GraphQL
    endpoint host is deliberate: the host's first label happens to be the api
    id, but "happens to be" is not worth building on when the deployment
    already knows the answer, and the cost of being wrong is a log group name
    the agent cannot query.

    This returns a *candidate*. The caller confirms it against
    ``describe_log_groups`` before reporting it, so a deployment with AppSync
    logging switched off does not get a group name the agent will then fail to
    search. Deriving a candidate and verifying it is not the same thing as
    constructing a name and trusting it.
    """
    api_id = (api_id or "").strip()
    # AppSync api ids are lowercase base32. Anything else is not one, and a
    # value interpolated into a log group name is worth checking.
    if not re.fullmatch(r"[a-z0-9]{10,64}", api_id):
        return None
    return f"/aws/appsync/apis/{api_id}"


def stack_log_group_prefixes(
    stack_name: str, nested_stack_names: Optional[Sequence[str]] = None
) -> List[str]:
    """The prefixes under which this deployment's log groups can appear.

    **Ordered most specific first, and that ordering is load-bearing.** When
    the ``max_log_groups`` cap is reached pagination stops, so whatever is
    paged last is what gets dropped. The virtual participant's container group
    and the launch state machine's vended group are the two the prompt calls
    the most important, and both live in the ``/aws/``-rooted namespaces, so
    those are paged first. Leading with the broad stack prefix would drop
    exactly the evidence the agent most needs, and ``search_logs`` would then
    reject those names as not existing in this deployment.

    When ``nested_stack_names`` is supplied, each nested stack is matched by
    its own full name. Otherwise the parent stack's name is used as a prefix,
    which also matches any *other* stack in the account whose name begins with
    it — ``LMA`` matching ``LMA-dev``. The caller is told which of the two it
    got, because evidence from a neighbouring deployment attributed to this one
    is the worst answer this agent could give.
    """
    if not stack_name:
        return []
    prefixes: List[str] = []

    # Named exactly rather than by prefix: "/aws/lambda-microvms/LMA-" would
    # also match "LMA-dev-vp-logs". Both generations, current first, ahead of
    # every broad prefix: in the window after an upgrade the legacy group holds
    # the evidence for the participants most likely to be asked about.
    # discover_log_groups keeps only exact matches for these, because the
    # legacy "-vp" is also a prefix of "-vp-logs" and of a neighbouring
    # "LMA-vpn-vp-logs". The current names are matched exactly too, on purpose,
    # which is narrower than the IAM grant for them: that grant ends in "*",
    # which also covers the ":*" stream form of each group's ARN. Narrowing the
    # grant to match would need that form listed explicitly, as the legacy
    # entries do.
    prefixes.extend(microvm_log_group_names(stack_name))

    if nested_stack_names:
        # Every vended-states prefix before any nested-stack prefix. Within one
        # loop they would interleave in whatever order ListStackResources
        # returned, which can put the VP launch state machine's group after
        # several broad prefixes — and that group is the only record a launch
        # was attempted, so it must not be what a cap drops.
        for nested in nested_stack_names:
            prefixes.append(f"/aws/vendedlogs/states/{nested}")
        for nested in nested_stack_names:
            prefixes.append(f"/{nested}/")
    else:
        prefixes.append(f"/aws/vendedlogs/states/{stack_name}-")
        prefixes.append(f"/{stack_name}-")

    # Groups named after the parent stack rather than a nested one. Always
    # carries the trailing separator, so it cannot match a longer sibling name.
    prefixes.append(f"/{stack_name}/")
    return prefixes


@dataclass(frozen=True)
class DiscoveryResult:
    """Everything one discovery pass found, and what it could not reach."""

    groups: List[LogGroupInfo]
    truncated: bool
    scoped_to_nested_stacks: bool

    def caveats(self) -> List[str]:
        """Statements the agent must not reason past. Empty when there are none."""
        notes: List[str] = []
        legacy = [g.name for g in self.groups if g.generation == LEGACY_GENERATION]
        if legacy:
            # Carried in the tool result as well as in the shipped system
            # prompt: an operator's custom prompt replaces the shipped one
            # wholesale, so the prompt alone would not reach that deployment.
            notes.append(
                "This deployment was upgraded from a release that logged "
                "MicroVM output under older names, so it has legacy MicroVM "
                f"log groups ({', '.join(sorted(legacy))}) as well as, or "
                "instead of, the current ones. A participant whose MicroVM "
                "was launched before the upgrade finished logged ONLY to the "
                "legacy group, whichever image it ran on. If a current "
                "MicroVM group has no entries for the "
                "participant or meeting you are investigating, search the "
                "legacy group before concluding there is no evidence. The "
                "launch state machine's microvmId appears in the legacy "
                "group's stream names, so filter on @logStream with "
                "query_string to find it; if no stream matches, search the "
                "legacy group's message text for the participant's id before "
                "treating the miss as an absence."
            )
        if self.truncated:
            notes.append(
                "This list was cut short at the configured log group cap, so "
                "some of this deployment's log groups are missing from it and "
                "cannot be searched. Treat a group you expected and cannot see "
                "as absent from THIS LIST, not as absent from the deployment."
            )
        if not self.scoped_to_nested_stacks:
            notes.append(
                "This deployment's nested stack names could not be resolved, so "
                "groups were matched by stack-name prefix. If another stack in "
                "this account has a name beginning with this one's, its log "
                "groups may appear here. Check that a group you rely on belongs "
                "to this deployment before quoting it as evidence."
            )
        return notes


def discover_log_groups(
    logs_client: Any,
    stack_name: str,
    *,
    max_log_groups: int,
    appsync_api_id: str = "",
    nested_stack_names: Optional[Sequence[str]] = None,
) -> DiscoveryResult:
    """Page `describe_log_groups` over every namespace this deployment uses.

    Deliberately returns everything it finds rather than taking a filter:
    callers cache this result and filter it afterwards, so a filtered pass can
    never be what populates a cache that a later unfiltered read depends on.

    Args:
        logs_client: A CloudWatch Logs client.
        stack_name: The LMA parent stack name.
        max_log_groups: Hard cap on the number returned.
        appsync_api_id: This deployment's AppSync API id, used to look for
            the service-managed AppSync group. Optional.
        nested_stack_names: This deployment's nested stacks' physical names.
            Supplying them is what makes the match exact rather than by prefix.

    Returns:
        A DiscoveryResult whose groups are sorted by component then name, so
        related groups sit adjacent in the model's context, and which reports
        whether the list is complete and whether it covers this deployment
        alone.
    """
    found: Dict[str, LogGroupInfo] = {}
    truncated = False

    prefixes = stack_log_group_prefixes(stack_name, nested_stack_names)
    microvm_names = microvm_log_group_names(stack_name)
    candidate = appsync_log_group_candidate(appsync_api_id)
    if candidate:
        # A full group name is also a valid prefix, which is how the candidate
        # gets verified rather than assumed. Inserted at the front with the
        # other narrow prefixes rather than appended: appending would make it
        # the first group a cap drops, which is the ordering rule
        # stack_log_group_prefixes exists to state.
        prefixes.insert(0, candidate)

    paginator = logs_client.get_paginator("describe_log_groups")
    for prefix in prefixes:
        if truncated:
            break
        # A MicroVM prefix is a full group name and is matched exactly; see
        # stack_log_group_prefixes. The longer names it also returns are
        # skipped without counting toward the cap, so paging for it stops as
        # soon as the exact name has been seen rather than running on through
        # them. When the exact name does not exist, paging is bounded by how
        # many other groups share the prefix, which is few.
        exact = prefix in microvm_names
        for page in paginator.paginate(logGroupNamePrefix=prefix):
            for group in page.get("logGroups", []):
                name = group.get("logGroupName")
                if not name or name in found:
                    continue
                if exact and name != prefix:
                    continue
                if len(found) >= max_log_groups:
                    # Stop paginating rather than collecting and slicing: the
                    # cap bounds the API calls as well as the result. The flag
                    # is what keeps the shortfall from reading as an absence.
                    truncated = True
                    break
                found[name] = LogGroupInfo(
                    name=name,
                    component=classify_log_group(name),
                    stored_bytes=int(group.get("storedBytes") or 0),
                    retention_days=group.get("retentionInDays"),
                    generation=microvm_names.get(name),
                )
            if truncated or (exact and prefix in found):
                break

    return DiscoveryResult(
        groups=_sorted(found.values()),
        truncated=truncated,
        scoped_to_nested_stacks=bool(nested_stack_names),
    )


def _sorted(groups: Iterable[LogGroupInfo]) -> List[LogGroupInfo]:
    return sorted(groups, key=lambda g: (g.component, g.name))
