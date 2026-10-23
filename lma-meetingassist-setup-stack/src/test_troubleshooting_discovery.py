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
APPSYNC_URL = "https://abcdefghijklmnopqrstuvwxyz.appsync-api.us-east-1.amazonaws.com/graphql"
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


def discover(client: Any = None, **kwargs: Any) -> List[discovery.LogGroupInfo]:
    options: Dict[str, Any] = {"max_log_groups": 500, "graphql_url": APPSYNC_URL}
    options.update(kwargs)
    return discovery.discover_log_groups(client or FakeLogsClient(), STACK, **options)


def by_name(groups: List[discovery.LogGroupInfo]) -> Dict[str, discovery.LogGroupInfo]:
    return {g.name: g for g in groups}


# ── prefixes ────────────────────────────────────────────────────────────────


def test_all_four_stack_namespaces_are_searched() -> None:
    """Three of LMA's log groups are unreachable from the obvious two prefixes.

    The MicroVM groups and the state machine's vended group are rooted under
    /aws/, so a discovery that paginates only /<stack>- and /<stack>/ misses
    the VP container's logs entirely -- which under the default launch type is
    the only place a join attempt is recorded.
    """
    assert discovery.stack_log_group_prefixes(STACK) == [
        f"/{STACK}-",
        f"/{STACK}/",
        f"/aws/lambda-microvms/{STACK}-",
        f"/aws/vendedlogs/states/{STACK}-",
    ]


def test_no_prefixes_are_offered_for_an_unnamed_stack() -> None:
    assert discovery.stack_log_group_prefixes("") == []


def test_every_real_log_group_is_found() -> None:
    found = by_name(discover())

    assert set(found) == {name for name, _ in REAL_GROUPS}


def test_pagination_is_followed() -> None:
    """The fake splits every prefix across two pages; nothing may be dropped."""
    found = discover()

    assert len(found) == len(REAL_GROUPS)


def test_a_group_reachable_from_two_prefixes_is_reported_once() -> None:
    client = FakeLogsClient(
        [(f"/{STACK}/lambda/Thing", 1), (f"/{STACK}-NESTED/lambda/Thing", 1)]
    )

    assert len(discover(client)) == 2


# ── the AppSync group ───────────────────────────────────────────────────────


def test_the_appsync_group_is_derived_from_the_endpoint_and_then_verified() -> None:
    """Deriving a candidate and confirming it is not the same as guessing.

    AppSync's group is named after the API id, which is in the endpoint the
    function already holds; it is then looked up, so a deployment with AppSync
    logging off reports no group rather than an unqueryable name.
    """
    assert discovery.appsync_log_group_candidate(APPSYNC_URL) == APPSYNC_GROUP
    assert APPSYNC_GROUP in by_name(discover())


def test_the_appsync_group_is_not_reported_when_it_does_not_exist() -> None:
    client = FakeLogsClient([(f"/{AISTACK}/lambda/CallEventProcessorFunction", 1)])

    assert APPSYNC_GROUP not in by_name(discover(client))


@pytest.mark.parametrize(
    "url",
    [
        "",
        "not a url",
        "https://example.com/graphql",
        "https://short.appsync-api.us-east-1.amazonaws.com/graphql",
        # A hyphen cannot appear in an AppSync api id, so a host whose first
        # label carries one is some other service's endpoint.
        "https://my-own-gateway-endpoint.example.com/graphql",
        # Too long to be an api id.
        "https://" + "a" * 40 + ".appsync-api.us-east-1.amazonaws.com/graphql",
    ],
)
def test_a_url_that_is_not_an_appsync_endpoint_yields_no_candidate(url: str) -> None:
    assert discovery.appsync_log_group_candidate(url) is None


def test_the_api_id_is_read_case_insensitively_as_dns_requires() -> None:
    """Hostnames are case-insensitive, so the endpoint's case must not matter.

    AppSync api ids are lowercase, but an operator who typed the endpoint with
    different case into a parameter should still get the right log group.
    """
    assert (
        discovery.appsync_log_group_candidate(
            "https://ABCDEFGHIJKLMNOPQRSTUVWXYZ.appsync-api.us-east-1.amazonaws.com/graphql"
        )
        == APPSYNC_GROUP
    )


def test_discovery_works_without_an_appsync_endpoint() -> None:
    found = by_name(discover(graphql_url=""))

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


def test_the_microvm_suffixes_are_matched_as_they_are_spelled_today() -> None:
    """The MicroVM groups were renamed from -vp/-asr to -vp-logs/-asr-logs.

    An agent carrying either spelling as a literal would report "no logs" on a
    deployment using the other, which is why these names are discovered rather
    than built. This test pins only the classifier's reading of the current
    spelling; nothing in the tools constructs it.
    """
    assert (
        discovery.classify_log_group(f"/aws/lambda-microvms/{STACK}-vp-logs")
        == discovery.VIRTUAL_PARTICIPANT
    )
    assert (
        discovery.classify_log_group(f"/aws/lambda-microvms/{STACK}-asr-logs")
        == discovery.ASR_MICROVM
    )


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
    }


# ── caps and ordering ───────────────────────────────────────────────────────


def test_the_cap_bounds_the_result() -> None:
    assert len(discover(max_log_groups=5)) == 5


def test_the_cap_stops_paginating_rather_than_slicing_afterwards() -> None:
    """The cap bounds the API calls as well as the result."""
    client = FakeLogsClient()
    discover(client, max_log_groups=1)

    assert len(client.prefixes_requested) == 1


def test_groups_are_grouped_by_component_so_related_ones_are_adjacent() -> None:
    components = [g.component for g in discover()]

    assert components == sorted(components)
