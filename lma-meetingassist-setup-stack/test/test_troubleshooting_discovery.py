#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""Unit tests for log group discovery and component labelling (no AWS).

The fixtures below are the real log group names LMA's templates produce, with
a representative generated nested-stack name substituted for AWS::StackName.
They are the point of the suite: a classifier built from invented names would
pass its own tests and mislabel a real deployment.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest
import troubleshooting_discovery as discovery

STACK = "LMA-Test"
AISTACK = f"{STACK}-AISTACK-1A2B3C4D5E6F"
VPSTACK = f"{STACK}-VIRTUALPARTICIPANTSTACK-9Z8Y7X"
TRANSCRIBERSTACK = f"{STACK}-WEBSOCKETTRANSCRIBERSTACK-Q1W2E3"
APPSYNC_API_ID = "abcdefghijklmnopqrstuvwxyz"
APPSYNC_GROUP = "/aws/appsync/apis/abcdefghijklmnopqrstuvwxyz"

# (name, storedBytes). Mirrors what describe_log_groups returns.
REAL_GROUPS: List[tuple[str, int]] = [
    # Nested-stack namespace -- the overwhelming majority.
    (f"/{AISTACK}/lambda/CallEventProcessorFunction", 4096),
    (f"/{AISTACK}/lambda/StaleMeetingReaperFunction", 512),
    (f"/{AISTACK}/lambda/SpeakerNameResolverFunction", 256),
    (f"/{AISTACK}/lambda/StrandsMeetingAssistFunction", 2048),
    (f"/{AISTACK}/lambda/BedrockSummaryLambda", 1024),
    (f"/{AISTACK}/lambda/VirtualParticipantManager", 700),
    (f"/{AISTACK}/lambda/VPScheduler", 128),
    (f"/{AISTACK}/lambda/VPTaskReaper", 64),
    (f"/{AISTACK}/lambda/VoiceEnrollmentManagerFunction", 32),
    (f"/{AISTACK}/lambda/CognitoCustomMessageFunction", 16),
    (f"/{AISTACK}/lambda/LLMPromptUploadFunction", 8),
    (f"/{TRANSCRIBERSTACK}/fargate/TranscriberWebsocket", 999999),
    # Empty under the default VPLaunchType=MICROVM. This is the entry the
    # hasData flag exists for.
    (f"/{VPSTACK}/fargate/VirtualParticipant", 0),
    (f"/aws/vendedlogs/states/{VPSTACK}", 333),
    # Parent-stack namespace -- a handful of groups use this instead.
    (f"/{STACK}/lambda/MCPServerAnalytics", 10),
    (f"/{STACK}/lambda/VirtualParticipantManager", 20),
    # Outside the stack-name namespace altogether.
    (f"/aws/lambda-microvms/{STACK}-vp-logs", 88888),
    (f"/aws/lambda-microvms/{STACK}-asr-logs", 0),
    # Service-managed, named after the API id.
    (APPSYNC_GROUP, 77),
]


class FakeLogsClient:
    """Serves describe_log_groups by prefix, recording what was asked for."""

    def __init__(self, groups: Optional[List[tuple[str, int]]] = None):
        self._groups = REAL_GROUPS if groups is None else groups
        self.prefixes_requested: List[str] = []

    def get_paginator(self, operation: str) -> "FakeLogsClient":
        assert operation == "describe_log_groups"
        return self

    def paginate(self, logGroupNamePrefix: str) -> List[Dict[str, Any]]:  # noqa: N803
        self.prefixes_requested.append(logGroupNamePrefix)
        matching = [
            {
                "logGroupName": name,
                "storedBytes": size,
                "retentionInDays": 90,
            }
            for name, size in self._groups
            if name.startswith(logGroupNamePrefix)
        ]
        # Two pages, to exercise pagination rather than assume one page.
        return [{"logGroups": matching[:1]}, {"logGroups": matching[1:]}]


#: The nested stack names a resolved deployment would report. Supplying these
#: is what makes discovery exact rather than prefix-matched.
NESTED_STACKS = [AISTACK, VPSTACK, TRANSCRIBERSTACK]


def discover_result(client: Any = None, **kwargs: Any) -> discovery.DiscoveryResult:
    options: Dict[str, Any] = {
        "max_log_groups": 500,
        "appsync_api_id": APPSYNC_API_ID,
        "nested_stack_names": NESTED_STACKS,
    }
    options.update(kwargs)
    return discovery.discover_log_groups(client or FakeLogsClient(), STACK, **options)


def discover(client: Any = None, **kwargs: Any) -> List[discovery.LogGroupInfo]:
    return discover_result(client, **kwargs).groups


def by_name(groups: List[discovery.LogGroupInfo]) -> Dict[str, discovery.LogGroupInfo]:
    return {g.name: g for g in groups}


# ── prefixes ────────────────────────────────────────────────────────────────


def test_every_namespace_is_searched_including_the_aws_rooted_ones() -> None:
    """Three of LMA's log groups are unreachable from the obvious two prefixes.

    The MicroVM groups and the state machine's vended group are rooted under
    /aws/, so a discovery that paginates only /<stack>- and /<stack>/ misses
    the VP container's logs entirely -- which under the default launch type is
    the only place a join attempt is recorded.
    """
    prefixes = discovery.stack_log_group_prefixes(STACK, NESTED_STACKS)

    assert f"/aws/lambda-microvms/{STACK}-vp-logs" in prefixes
    assert f"/aws/lambda-microvms/{STACK}-asr-logs" in prefixes
    assert f"/aws/vendedlogs/states/{VPSTACK}" in prefixes
    assert f"/{AISTACK}/" in prefixes
    assert f"/{STACK}/" in prefixes


def test_the_aws_rooted_prefixes_are_searched_first() -> None:
    """Ordering decides what a cap drops, and the two most valuable groups --
    the VP container's and the launch state machine's -- are the /aws/ ones.
    Leading with the broad stack prefix would drop exactly those."""
    prefixes = discovery.stack_log_group_prefixes(STACK, NESTED_STACKS)

    assert prefixes[0].startswith("/aws/lambda-microvms/")
    assert prefixes.index(f"/aws/vendedlogs/states/{VPSTACK}") < prefixes.index(
        f"/{STACK}/"
    )


def test_a_resolved_deployment_matches_each_nested_stack_by_name() -> None:
    """Not by the parent's prefix: "/LMA-" also matches "LMA-dev-AISTACK-x", so
    a neighbouring deployment's groups would be listed as this one's evidence.
    """
    prefixes = discovery.stack_log_group_prefixes(STACK, NESTED_STACKS)

    assert f"/{STACK}-" not in prefixes
    for nested in NESTED_STACKS:
        assert f"/{nested}/" in prefixes


def test_the_parent_prefix_always_carries_its_separator() -> None:
    """So that "/LMA/" cannot match a group belonging to "LMA-dev"."""
    for nested in (NESTED_STACKS, None):
        assert f"/{STACK}/" in discovery.stack_log_group_prefixes(STACK, nested)


def test_an_unresolved_deployment_falls_back_to_the_parent_prefix() -> None:
    """Degraded but usable, and the result says so -- see the caveat tests."""
    prefixes = discovery.stack_log_group_prefixes(STACK, None)

    assert f"/{STACK}-" in prefixes
    assert f"/aws/vendedlogs/states/{STACK}-" in prefixes


def test_no_prefixes_are_offered_for_an_unnamed_stack() -> None:
    assert discovery.stack_log_group_prefixes("") == []
    assert discovery.stack_log_group_prefixes("", NESTED_STACKS) == []


def test_every_real_log_group_is_found() -> None:
    found = by_name(discover())

    assert set(found) == {name for name, _ in REAL_GROUPS}


def test_pagination_is_followed() -> None:
    """The fake splits every prefix across two pages; nothing may be dropped."""
    found = discover()

    assert len(found) == len(REAL_GROUPS)


def test_a_group_reachable_from_two_prefixes_is_reported_once() -> None:
    client = FakeLogsClient(
        [(f"/{STACK}/lambda/Thing", 1), (f"/{AISTACK}/lambda/Thing", 1)]
    )

    assert len(discover(client)) == 2


# ── scoping to this deployment ──────────────────────────────────────────────


def test_a_neighbouring_deployments_groups_are_not_listed_as_this_ones() -> None:
    """An account can hold both "LMA-Test" and "LMA-Test-dev". Quoting the
    second one's log line as the first one's evidence is the worst answer this
    agent could give, so nested stacks are matched by name."""
    neighbour = f"{STACK}-dev-AISTACK-DEADBEEF"
    client = FakeLogsClient(
        REAL_GROUPS + [(f"/{neighbour}/lambda/CallEventProcessorFunction", 999)]
    )

    found = by_name(discover(client))

    assert f"/{neighbour}/lambda/CallEventProcessorFunction" not in found
    assert f"/{AISTACK}/lambda/CallEventProcessorFunction" in found


def test_a_neighbouring_microvm_group_is_not_listed_either() -> None:
    """"/aws/lambda-microvms/LMA-Test-" would match "LMA-Test-dev-vp-logs", so
    those two groups are named exactly rather than by prefix."""
    client = FakeLogsClient(
        REAL_GROUPS + [(f"/aws/lambda-microvms/{STACK}-dev-vp-logs", 999)]
    )

    found = by_name(discover(client))

    assert f"/aws/lambda-microvms/{STACK}-dev-vp-logs" not in found
    assert f"/aws/lambda-microvms/{STACK}-vp-logs" in found


def test_a_resolved_discovery_reports_that_it_is_scoped() -> None:
    assert discover_result().scoped_to_nested_stacks is True
    assert discover_result().caveats() == []


def test_an_unresolved_discovery_says_a_neighbour_may_be_included() -> None:
    """Degrading is acceptable; degrading silently is not. The agent is told,
    so it can check a name before quoting it."""
    result = discover_result(nested_stack_names=None)

    assert result.scoped_to_nested_stacks is False
    caveats = " ".join(result.caveats())
    assert "matched by stack-name prefix" in caveats
    assert "may appear here" in caveats


# ── the AppSync group ───────────────────────────────────────────────────────


def test_the_appsync_group_is_named_from_the_stacks_api_id_and_verified() -> None:
    """Deriving a candidate and confirming it is not the same as guessing.

    AppSync's group is named after the API id, which the deployment passes in
    as a parameter -- the same value the IAM grant for that group is scoped
    to, so the two cannot disagree. The name is then looked up, so a
    deployment with AppSync logging off reports no group rather than a name
    the agent will fail to search.
    """
    assert discovery.appsync_log_group_candidate(APPSYNC_API_ID) == APPSYNC_GROUP
    assert APPSYNC_GROUP in by_name(discover())


def test_the_appsync_group_is_not_reported_when_it_does_not_exist() -> None:
    client = FakeLogsClient([(f"/{AISTACK}/lambda/CallEventProcessorFunction", 1)])

    assert APPSYNC_GROUP not in by_name(discover(client))


@pytest.mark.parametrize(
    "api_id",
    [
        "",
        "   ",
        # Not an api id: these would be interpolated into a log group name.
        "short",
        "has-a-hyphen-in-it",
        "has/a/slash",
        "HasUpperCase",
        "a" * 100,
        "../../etc/passwd",
    ],
)
def test_a_value_that_is_not_an_api_id_yields_no_candidate(api_id: str) -> None:
    """A parameter value interpolated into a log group name is worth checking,
    and an absent id means no AppSync group rather than a guessed one."""
    assert discovery.appsync_log_group_candidate(api_id) is None


def test_surrounding_whitespace_on_the_api_id_is_tolerated() -> None:
    assert (
        discovery.appsync_log_group_candidate(f"  {APPSYNC_API_ID}  ") == APPSYNC_GROUP
    )


def test_discovery_works_without_an_appsync_api_id() -> None:
    found = by_name(discover(appsync_api_id=""))

    assert APPSYNC_GROUP not in found
    assert f"/{AISTACK}/lambda/CallEventProcessorFunction" in found


# ── classification ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        # The VP container under each launch type. Both must land on the same
        # component, or an agent told "look at the virtual participant" would
        # be handed the wrong group for its deployment.
        (f"/aws/lambda-microvms/{STACK}-vp-logs", discovery.VIRTUAL_PARTICIPANT),
        (f"/{VPSTACK}/fargate/VirtualParticipant", discovery.VIRTUAL_PARTICIPANT),
        (f"/{AISTACK}/lambda/VirtualParticipantManager", discovery.VIRTUAL_PARTICIPANT),
        (f"/{STACK}/lambda/VirtualParticipantManager", discovery.VIRTUAL_PARTICIPANT),
        (f"/{AISTACK}/lambda/VPScheduler", discovery.VIRTUAL_PARTICIPANT),
        (f"/{AISTACK}/lambda/VPTaskReaper", discovery.VIRTUAL_PARTICIPANT),
        (f"/{AISTACK}/lambda/VPMicrovmLauncherFunction", discovery.VIRTUAL_PARTICIPANT),
        (f"/aws/vendedlogs/states/{VPSTACK}", discovery.VP_LAUNCH_STATE_MACHINE),
        (f"/{TRANSCRIBERSTACK}/fargate/TranscriberWebsocket", discovery.WEBSOCKET_TRANSCRIBER),
        (f"/aws/lambda-microvms/{STACK}-asr-logs", discovery.ASR_MICROVM),
        (f"/{AISTACK}/lambda/AsrLauncherFunction", discovery.ASR_MICROVM),
        (f"/{AISTACK}/lambda/CallEventProcessorFunction", discovery.TRANSCRIPT_PIPELINE),
        (f"/{AISTACK}/lambda/StaleMeetingReaperFunction", discovery.TRANSCRIPT_PIPELINE),
        (f"/{AISTACK}/lambda/SpeakerNameResolverFunction", discovery.TRANSCRIPT_PIPELINE),
        (f"/{AISTACK}/lambda/StrandsMeetingAssistFunction", discovery.MEETING_ASSISTANT),
        # The agent's own log group, so "why did the last diagnosis fail" is
        # answerable by component like anything else.
        (
            f"/{STACK}-MEETINGASSISTSETUP-X1Y2/lambda/TroubleshootingAgentFunction",
            discovery.MEETING_ASSISTANT,
        ),
        (f"/{AISTACK}/lambda/BedrockSummaryLambda", discovery.MEETING_ASSISTANT),
        (f"/{STACK}/lambda/MCPServerAnalytics", discovery.MEETING_ASSISTANT),
        (APPSYNC_GROUP, discovery.GRAPHQL_API),
        (f"/{AISTACK}/lambda/CognitoCustomMessageFunction", discovery.AUTH),
        (f"/{AISTACK}/lambda/VoiceEnrollmentManagerFunction", discovery.VOICE_ENROLLMENT),
        (f"/{AISTACK}/lambda/LLMPromptUploadFunction", discovery.DEPLOYMENT),
        ("/some/unrelated/group", discovery.OTHER),
    ],
)
def test_a_log_group_is_attributed_to_the_right_component(name: str, expected: str) -> None:
    assert discovery.classify_log_group(name) == expected


@pytest.mark.parametrize(
    ("suffix", "expected"),
    [
        ("-vp-logs", discovery.VIRTUAL_PARTICIPANT),
        ("-asr-logs", discovery.ASR_MICROVM),
        ("-vp", discovery.VIRTUAL_PARTICIPANT),
        ("-asr", discovery.ASR_MICROVM),
    ],
)
def test_both_generations_of_microvm_group_land_on_the_same_component(
    suffix: str, expected: str
) -> None:
    """Images built before v0.3.11 log to -vp/-asr, later ones to -vp-logs and
    -asr-logs. Filtering discovery by component has to return both, or the
    agent asking for "the virtual participant's logs" is handed only one."""
    assert discovery.classify_log_group(f"/aws/lambda-microvms/{STACK}{suffix}") == expected


def test_every_component_the_classifier_can_return_has_a_note() -> None:
    """The notes are what tell the agent which group answers which question."""
    produced = {discovery.classify_log_group(name) for name, _ in REAL_GROUPS}

    assert produced <= set(discovery.COMPONENT_NOTES)
    assert set(discovery.COMPONENT_NOTES) >= produced


def test_discovery_takes_no_filter_of_its_own() -> None:
    """Filtering belongs to the caller, which caches this result.

    A filtered pass must never be what populates that cache, or a later
    unfiltered read would see only the components somebody asked about first.
    The filter therefore lives in the tool; see test_troubleshooting_tools.
    """
    import inspect

    parameters = inspect.signature(discovery.discover_log_groups).parameters

    assert "components" not in parameters


# ── stored bytes ────────────────────────────────────────────────────────────


def test_a_group_that_has_never_been_written_to_is_flagged() -> None:
    """Under the default launch type the Fargate group exists and is empty.

    Reporting that as 'no evidence the VP ran' is the most likely way for this
    agent to be confidently wrong, so the flag is part of every result.
    """
    found = by_name(discover())

    assert found[f"/{VPSTACK}/fargate/VirtualParticipant"].has_data is False
    assert found[f"/aws/lambda-microvms/{STACK}-vp-logs"].has_data is True


def test_an_absent_stored_bytes_field_counts_as_no_data() -> None:
    class Sparse(FakeLogsClient):
        def paginate(self, logGroupNamePrefix: str):  # noqa: N803
            self.prefixes_requested.append(logGroupNamePrefix)
            if logGroupNamePrefix != f"/{STACK}/":
                return [{"logGroups": []}]
            return [{"logGroups": [{"logGroupName": f"/{STACK}/lambda/Thing"}]}]

    found = discover(Sparse())

    assert found[0].has_data is False
    assert found[0].retention_days is None


def test_the_reported_shape_carries_what_the_agent_needs_to_route() -> None:
    group = by_name(discover())[f"/aws/lambda-microvms/{STACK}-vp-logs"]

    assert group.to_dict() == {
        "logGroupName": f"/aws/lambda-microvms/{STACK}-vp-logs",
        "component": discovery.VIRTUAL_PARTICIPANT,
        "storedBytes": 88888,
        "hasData": True,
        "retentionDays": 90,
        "microvmLogGeneration": discovery.CURRENT_GENERATION,
        "note": discovery._GENERATION_NOTES[discovery.CURRENT_GENERATION],
    }


def test_a_group_that_has_only_ever_had_one_name_carries_no_generation() -> None:
    """The label is read by the model; a null on every other group is noise."""
    reported = by_name(discover())[f"/{AISTACK}/lambda/CallEventProcessorFunction"].to_dict()

    assert "microvmLogGeneration" not in reported
    assert "note" not in reported


# ── MicroVM log group generations ───────────────────────────────────────────
#
# A stack upgraded from before v0.3.11 keeps the -vp/-asr groups its earlier
# images logged to, alongside the -vp-logs/-asr-logs groups it now declares. A
# participant that ran on the pre-upgrade image logged only to the former.

CURRENT_VP = f"/aws/lambda-microvms/{STACK}-vp-logs"
CURRENT_ASR = f"/aws/lambda-microvms/{STACK}-asr-logs"
LEGACY_VP = f"/aws/lambda-microvms/{STACK}-vp"
LEGACY_ASR = f"/aws/lambda-microvms/{STACK}-asr"
NOT_MICROVM = [(n, s) for n, s in REAL_GROUPS if "/aws/lambda-microvms/" not in n]


def generations(result: discovery.DiscoveryResult) -> Dict[str, Optional[str]]:
    return {g.name: g.generation for g in result.groups if g.generation}


def legacy_caveat(result: discovery.DiscoveryResult) -> List[str]:
    return [c for c in result.caveats() if "legacy MicroVM log groups" in c]


def test_an_upgraded_deployment_reports_both_generations_labelled() -> None:
    groups = NOT_MICROVM + [
        (CURRENT_VP, 500),
        (CURRENT_ASR, 0),
        (LEGACY_VP, 9000),
        (LEGACY_ASR, 40),
    ]

    result = discover_result(FakeLogsClient(groups))

    assert generations(result) == {
        CURRENT_VP: discovery.CURRENT_GENERATION,
        CURRENT_ASR: discovery.CURRENT_GENERATION,
        LEGACY_VP: discovery.LEGACY_GENERATION,
        LEGACY_ASR: discovery.LEGACY_GENERATION,
    }
    found = by_name(result.groups)
    assert found[LEGACY_VP].component == discovery.VIRTUAL_PARTICIPANT
    assert found[LEGACY_ASR].component == discovery.ASR_MICROVM
    legacy_note = found[LEGACY_VP].to_dict()["note"]
    assert "before v0.3.11" in legacy_note
    assert "microvmId" in legacy_note
    # The caveat names the legacy groups so the agent cannot miss them, and
    # tells it how to find a participant's stream in one.
    [caveat] = legacy_caveat(result)
    assert LEGACY_VP in caveat and LEGACY_ASR in caveat
    assert CURRENT_VP not in caveat
    assert "@logStream" in caveat


def test_a_fresh_deployment_reports_only_the_current_generation() -> None:
    """It never had the legacy groups, so none is listed and nothing warns."""
    result = discover_result()

    assert generations(result) == {
        CURRENT_VP: discovery.CURRENT_GENERATION,
        CURRENT_ASR: discovery.CURRENT_GENERATION,
    }
    assert LEGACY_VP not in by_name(result.groups)
    assert legacy_caveat(result) == []


def test_a_deployment_with_only_the_legacy_generation_reports_it() -> None:
    """E.g. an upgraded stack later moved off VPLaunchType=MICROVM or with
    EnableMicrovmAsr turned off: the current groups are conditional and go with
    the feature (unless retained), while the legacy ones, which the stack never
    owned, stay. Whatever exists is reported, and nothing is assumed."""
    groups = NOT_MICROVM + [(LEGACY_VP, 9000), (LEGACY_ASR, 40)]

    result = discover_result(FakeLogsClient(groups))

    assert generations(result) == {
        LEGACY_VP: discovery.LEGACY_GENERATION,
        LEGACY_ASR: discovery.LEGACY_GENERATION,
    }
    assert CURRENT_VP not in by_name(result.groups)
    assert len(legacy_caveat(result)) == 1


def test_the_legacy_names_are_looked_up_not_assumed() -> None:
    client = FakeLogsClient()

    discover(client)

    assert LEGACY_VP in client.prefixes_requested
    assert LEGACY_ASR in client.prefixes_requested


def test_the_legacy_lookup_matches_its_name_exactly() -> None:
    """"-vp" is a prefix of a neighbouring "-vpn" stack's groups, and of
    anything else beginning with it; only the exact name is this deployment's."""
    neighbours = [
        f"/aws/lambda-microvms/{STACK}-vpn-vp-logs",
        f"/aws/lambda-microvms/{STACK}-vpc",
        f"/aws/lambda-microvms/{STACK}-asr2-asr-logs",
    ]
    groups = NOT_MICROVM + [(LEGACY_VP, 1), (LEGACY_ASR, 1)] + [(n, 5) for n in neighbours]

    found = by_name(discover(FakeLogsClient(groups)))

    assert LEGACY_VP in found and LEGACY_ASR in found
    for name in neighbours:
        assert name not in found


def test_the_current_group_is_labelled_current_whichever_prefix_finds_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The legacy name is also a prefix of the current one, so paging can reach
    the current group from either lookup; its label follows its name, not the
    lookup that returned it."""
    real = discovery.stack_log_group_prefixes
    monkeypatch.setattr(
        discovery,
        "stack_log_group_prefixes",
        lambda *args, **kwargs: list(reversed(real(*args, **kwargs))),
    )
    client = FakeLogsClient(NOT_MICROVM + [(CURRENT_VP, 5), (LEGACY_VP, 5)])

    result = discover_result(client)

    assert client.prefixes_requested.index(LEGACY_VP) < client.prefixes_requested.index(
        CURRENT_VP
    )
    assert generations(result)[CURRENT_VP] == discovery.CURRENT_GENERATION
    assert generations(result)[LEGACY_VP] == discovery.LEGACY_GENERATION


def test_the_legacy_groups_are_paged_before_the_broad_prefixes() -> None:
    """When the cap is reached, whatever is paged last is dropped; in the
    window after an upgrade the legacy group holds the evidence most asked for."""
    prefixes = discovery.stack_log_group_prefixes(STACK, NESTED_STACKS)

    first_broad = prefixes.index(f"/aws/vendedlogs/states/{AISTACK}")
    assert prefixes.index(LEGACY_VP) < first_broad
    assert prefixes.index(LEGACY_ASR) < first_broad


# ── caps and ordering ───────────────────────────────────────────────────────


def test_the_cap_bounds_the_result() -> None:
    assert len(discover(max_log_groups=5)) == 5


def test_the_cap_stops_paginating_rather_than_slicing_afterwards() -> None:
    """The cap bounds the API calls as well as the result.

    One prefix past the cap is still requested, which is how the shortfall is
    detected at all: the flag is set on meeting a group there is no room for,
    so something has to be found that does not fit.
    """
    client = FakeLogsClient()
    all_prefixes = discovery.stack_log_group_prefixes(STACK, NESTED_STACKS)

    discover(client, max_log_groups=1)

    assert len(client.prefixes_requested) < len(all_prefixes)


def test_a_truncated_list_says_so() -> None:
    """Otherwise the shortfall reads as an absence: search_logs would reject
    the dropped names as not existing in this deployment, and an empty result
    from a name that was never searched is indistinguishable from a component
    that produced nothing."""
    result = discover_result(max_log_groups=3)

    assert result.truncated is True
    caveats = " ".join(result.caveats())
    assert "cut short" in caveats
    assert "not as absent from the deployment" in caveats


def test_a_complete_list_carries_no_truncation_caveat() -> None:
    result = discover_result()

    assert result.truncated is False
    assert not any("cut short" in note for note in result.caveats())


def test_a_truncated_list_keeps_the_most_valuable_groups() -> None:
    """The cap drops whatever is paged last, so the ordering has to put the VP
    container's group and the launch state machine's first. These are the two
    the prompt calls the most important and the two that, under the default
    launch type, hold the only record a join was attempted."""
    kept = {g.name for g in discover(max_log_groups=3)}

    assert f"/aws/lambda-microvms/{STACK}-vp-logs" in kept


def test_groups_are_grouped_by_component_so_related_ones_are_adjacent() -> None:
    components = [g.component for g in discover()]

    assert components == sorted(components)
