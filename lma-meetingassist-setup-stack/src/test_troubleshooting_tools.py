#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""Unit tests for the four evidence tools (no AWS).

Weighted deliberately towards the log search and the record read. Those two
are the simplest tools here and so the easiest to assume are fine, and they
are also the two the agent calls on every single run and the two that spend
money — a Logs Insights query bills by bytes scanned. The complicated tool is
not the risky one; the frequently-called one is.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import pytest
from test_troubleshooting_discovery import (
    AISTACK,
    APPSYNC_URL,
    REAL_GROUPS,
    STACK,
    VPSTACK,
    FakeLogsClient,
)
from troubleshooting_config import AgentLimits
from troubleshooting_tools import (
    ToolError,
    TroubleshootingTools,
    _plain_value,
    build_search_query,
)

VP_TABLE = "LMA-Test-AISTACK-1A2B3C4D5E6F-VirtualParticipant"
VP_GROUP = f"/aws/lambda-microvms/{STACK}-vp-logs"
CEP_GROUP = f"/{AISTACK}/lambda/CallEventProcessorFunction"
STATE_MACHINE_GROUP = f"/aws/vendedlogs/states/{VPSTACK}"


class FakeInsightsClient(FakeLogsClient):
    """A logs client that also answers the Insights query lifecycle.

    `statuses` is consumed one entry per get_query_results call, so a test can
    make a query run for several polls before completing.
    """

    def __init__(
        self,
        rows: Optional[List[List[Dict[str, str]]]] = None,
        statuses: Optional[List[str]] = None,
        bytes_scanned: int = 4242,
        groups: Optional[List[tuple[str, int]]] = None,
    ):
        super().__init__(groups)
        self.rows = rows if rows is not None else []
        self.statuses = list(statuses) if statuses else ["Complete"]
        self.bytes_scanned = bytes_scanned
        self.start_query_calls: List[Dict[str, Any]] = []
        self.stopped: List[str] = []

    def start_query(self, **kwargs: Any) -> Dict[str, str]:
        self.start_query_calls.append(kwargs)
        return {"queryId": "query-1"}

    def get_query_results(self, queryId: str) -> Dict[str, Any]:  # noqa: N803
        assert queryId == "query-1"
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        if status != "Complete":
            return {"status": status, "results": []}
        return {
            "status": "Complete",
            "results": self.rows,
            "statistics": {"bytesScanned": self.bytes_scanned},
        }

    def stop_query(self, queryId: str) -> Dict[str, bool]:  # noqa: N803
        self.stopped.append(queryId)
        return {"success": True}


def row(**fields: str) -> List[Dict[str, str]]:
    """One Insights result row in the wire shape."""
    return [{"field": name, "value": value} for name, value in fields.items()]


def make_tools(
    logs_client: Any = None,
    dynamodb_client: Any = None,
    cloudformation_client: Any = None,
    *,
    stack_name: str = STACK,
    vp_table_name: str = VP_TABLE,
    limits: Optional[AgentLimits] = None,
    deadline: Optional[float] = None,
    clock: Optional[List[float]] = None,
) -> TroubleshootingTools:
    """Build the tools with fakes. `clock` is a mutable [now] a test can move."""
    ticks = clock if clock is not None else [0.0]
    return TroubleshootingTools(
        logs_client=logs_client or FakeInsightsClient(),
        dynamodb_client=dynamodb_client,
        cloudformation_client=cloudformation_client,
        stack_name=stack_name,
        vp_table_name=vp_table_name,
        limits=limits or AgentLimits(),
        graphql_url=APPSYNC_URL,
        deadline=deadline,
        sleep=lambda seconds: ticks.__setitem__(0, ticks[0] + seconds),
        monotonic=lambda: ticks[0],
    )


def parsed(result: str) -> Dict[str, Any]:
    return json.loads(result)


# ── discover_log_groups ─────────────────────────────────────────────────────


def test_discovery_reports_every_group_with_its_component_and_notes() -> None:
    result = parsed(make_tools().discover_log_groups())

    assert result["logGroupCount"] == len(REAL_GROUPS)
    assert result["stackName"] == STACK
    assert {g["logGroupName"] for g in result["logGroups"]} == {n for n, _ in REAL_GROUPS}
    assert "virtual-participant" in result["componentNotes"]


def test_discovery_counts_the_groups_that_hold_nothing() -> None:
    """Two of this deployment's groups are empty: the Fargate VP group under
    MICROVM, and the ASR group with the engine off."""
    result = parsed(make_tools().discover_log_groups())

    assert result["emptyLogGroupCount"] == 2
    assert "hasData=false" in result["guidance"]


def test_discovery_accepts_a_comma_separated_component_filter() -> None:
    """The model serialises a list argument as a string often enough to matter."""
    result = parsed(
        make_tools().discover_log_groups("vp-launch-state-machine,asr-microvm")
    )

    assert {g["component"] for g in result["logGroups"]} == {
        "vp-launch-state-machine",
        "asr-microvm",
    }


def test_discovery_is_cached_across_tool_calls_within_one_run() -> None:
    """The model asks for the log groups more than once; the answer is fixed."""
    client = FakeInsightsClient()
    tools = make_tools(client)

    tools.discover_log_groups()
    first = len(client.prefixes_requested)
    tools.discover_log_groups()

    assert len(client.prefixes_requested) == first


def test_a_filtered_discovery_does_not_poison_the_cache() -> None:
    """A filtered call must not leave search_logs believing only those exist."""
    tools = make_tools()
    tools.discover_log_groups("asr-microvm")

    assert len(parsed(tools.discover_log_groups())["logGroups"]) == len(REAL_GROUPS)


def test_a_search_still_works_after_a_filtered_discovery() -> None:
    """The consequence of the above, stated as the behaviour that matters: a
    group outside the filter must still be queryable afterwards."""
    client = FakeInsightsClient()
    tools = make_tools(client)
    tools.discover_log_groups("asr-microvm")

    result = parsed(tools.search_logs(VP_GROUP))

    assert result["logGroupNames"] == [VP_GROUP]


def test_filtering_on_an_unknown_component_returns_nothing_visibly() -> None:
    """An empty result the agent can see beats a silently unfiltered one."""
    result = parsed(make_tools().discover_log_groups("not-a-component"))

    assert result["logGroups"] == []
    assert result["logGroupCount"] == 0


# ── search_logs: query construction ─────────────────────────────────────────


def test_a_search_term_is_matched_as_a_literal_not_a_regex() -> None:
    """The terms worth searching for are bracket tags and log prefixes.

    `[TRANSCRIBING]` as a regex is a character class matching one letter from
    that set, which silently matches almost every line. Matching literally is
    what makes the obvious search work.
    """
    query = build_search_query(["[TRANSCRIBING]"], 10)

    assert '@message like "[TRANSCRIBING]"' in query


def test_several_terms_are_combined_with_or() -> None:
    query = build_search_query(["VP exit:", "trigger="], 10)

    assert '@message like "VP exit:" or @message like "trigger="' in query


def test_a_term_containing_a_quote_is_escaped() -> None:
    query = build_search_query(['say "hi"'], 10)

    assert '@message like "say \\"hi\\""' in query


def test_a_term_containing_a_backslash_is_escaped() -> None:
    query = build_search_query(["C:\\path"], 10)

    assert '@message like "C:\\\\path"' in query


def test_a_search_with_no_terms_returns_recent_lines() -> None:
    query = build_search_query([], 25)

    assert "filter" not in query
    assert "limit 25" in query


def test_the_query_returns_the_stream_name_as_well_as_the_message() -> None:
    """The VP and transcriber groups carry one stream per meeting, so the
    stream name is itself the correlation key -- and the output contract
    requires quoting it."""
    query = build_search_query(["x"], 10)

    assert "@logStream" in query
    assert "@timestamp" in query


def test_a_model_supplied_query_is_used_verbatim() -> None:
    """Aggregation is the reason this tool is Insights and not FilterLogEvents."""
    stats = 'fields @message | parse @message "trigger=*" as t | stats count() by t'
    client = FakeInsightsClient(rows=[row(t="pre-join:not-admitted", count="7")])

    result = parsed(
        make_tools(client).search_logs(VP_GROUP, query_string=stats)
    )

    assert result["queryString"] == stats
    assert client.start_query_calls[0]["queryString"] == stats
    assert result["rows"] == [{"t": "pre-join:not-admitted", "count": "7"}]


def test_a_model_supplied_query_wins_over_search_terms() -> None:
    client = FakeInsightsClient()

    make_tools(client).search_logs(
        VP_GROUP, search_terms="ignored", query_string="fields @message"
    )

    assert client.start_query_calls[0]["queryString"] == "fields @message"


def test_a_blank_query_string_falls_back_to_the_search_terms() -> None:
    client = FakeInsightsClient()

    make_tools(client).search_logs(VP_GROUP, search_terms="VP exit:", query_string="   ")

    assert 'like "VP exit:"' in client.start_query_calls[0]["queryString"]


# ── search_logs: log group validation ───────────────────────────────────────


def test_a_search_naming_no_log_group_is_refused() -> None:
    with pytest.raises(ToolError, match="discover_log_groups first"):
        make_tools().search_logs("")


def test_a_guessed_log_group_name_is_refused_rather_than_queried() -> None:
    """A guessed name returns an empty result that reads exactly like an
    absence of evidence, which is the most likely way for this agent to be
    confidently wrong. The names are not guessable: most are named after the
    generated nested-stack name, and the MicroVM groups sit outside the
    stack-name namespace entirely.
    """
    client = FakeInsightsClient()

    with pytest.raises(ToolError, match="none of the named log groups exist"):
        make_tools(client).search_logs(f"/aws/lambda/{STACK}-VirtualParticipant")

    assert client.start_query_calls == []


def test_a_search_naming_the_pre_rename_microvm_group_is_refused() -> None:
    """The MicroVM groups were -vp/-asr before they were -vp-logs/-asr-logs."""
    with pytest.raises(ToolError, match="none of the named log groups exist"):
        make_tools().search_logs(f"/aws/lambda-microvms/{STACK}-vp")


def test_the_known_names_are_queried_and_the_unknown_ones_reported() -> None:
    """A partly-wrong list still gathers evidence, and says what it dropped."""
    client = FakeInsightsClient()

    result = parsed(
        make_tools(client).search_logs(f"{VP_GROUP},/aws/lambda/invented")
    )

    assert result["logGroupNames"] == [VP_GROUP]
    assert result["unknownLogGroupNames"] == ["/aws/lambda/invented"]
    assert client.start_query_calls[0]["logGroupNames"] == [VP_GROUP]


def test_several_groups_are_correlated_in_one_query() -> None:
    """One query across two components is the other reason this is Insights."""
    client = FakeInsightsClient()

    make_tools(client).search_logs(f"{VP_GROUP},{CEP_GROUP},{STATE_MACHINE_GROUP}")

    assert client.start_query_calls[0]["logGroupNames"] == [
        VP_GROUP,
        CEP_GROUP,
        STATE_MACHINE_GROUP,
    ]


def test_a_log_group_list_is_accepted_as_a_list_too() -> None:
    client = FakeInsightsClient()

    make_tools(client).search_logs([VP_GROUP, CEP_GROUP])

    assert client.start_query_calls[0]["logGroupNames"] == [VP_GROUP, CEP_GROUP]


def test_the_number_of_groups_in_one_query_is_capped() -> None:
    """Logs Insights accepts at most 50 log groups in one query."""
    many = [(f"/{STACK}/lambda/F{i}", 10) for i in range(80)]
    client = FakeInsightsClient(groups=many)

    make_tools(client).search_logs(",".join(name for name, _ in many))

    assert len(client.start_query_calls[0]["logGroupNames"]) == 50


# ── search_logs: windows, limits and results ────────────────────────────────


def test_the_default_lookback_is_used_when_none_is_asked_for() -> None:
    client = FakeInsightsClient()
    limits = AgentLimits()

    result = parsed(make_tools(client, limits=limits).search_logs(VP_GROUP))

    assert result["lookbackHours"] == limits.default_lookback_hours
    call = client.start_query_calls[0]
    assert call["endTime"] - call["startTime"] == limits.default_lookback_hours * 3600


def test_a_lookback_beyond_the_ceiling_is_clamped() -> None:
    """Insights bills by bytes scanned, so this is the costliest knob here."""
    client = FakeInsightsClient()

    result = parsed(make_tools(client).search_logs(VP_GROUP, lookback_hours=100000))

    assert result["lookbackHours"] == AgentLimits().max_lookback_hours


def test_the_row_limit_is_capped_at_the_configured_maximum() -> None:
    client = FakeInsightsClient()

    make_tools(client).search_logs(VP_GROUP, limit=99999)

    assert client.start_query_calls[0]["limit"] == AgentLimits().max_log_events


def test_a_smaller_row_limit_is_honoured() -> None:
    client = FakeInsightsClient()

    make_tools(client).search_logs(VP_GROUP, limit=5)

    assert client.start_query_calls[0]["limit"] == 5


def test_more_rows_than_asked_for_are_trimmed() -> None:
    """The API's limit is advisory on some shapes; the cap is ours to keep."""
    client = FakeInsightsClient(rows=[row(m=str(i)) for i in range(40)])

    result = parsed(make_tools(client).search_logs(VP_GROUP, limit=3))

    assert result["rowCount"] == 3


def test_a_long_log_message_is_truncated_with_a_marker() -> None:
    """VP container lines carry serialised DOM snapshots of tens of kilobytes."""
    limits = AgentLimits()
    client = FakeInsightsClient(rows=[row(message="x" * 50000)])

    result = parsed(make_tools(client, limits=limits).search_logs(VP_GROUP))

    assert "truncated" in result["rows"][0]["message"]
    assert len(result["rows"][0]["message"]) < 50000
    assert result["rows"][0]["message"].startswith("x" * limits.max_log_message_length)


def test_the_opaque_record_pointer_is_dropped_from_results() -> None:
    """@ptr fetches the surrounding record, which this tool cannot do, so it
    is pure context cost."""
    client = FakeInsightsClient(rows=[row(**{"@ptr": "abc", "@message": "real"})])

    result = parsed(make_tools(client).search_logs(VP_GROUP))

    assert result["rows"] == [{"@message": "real"}]


def test_the_bytes_scanned_are_reported() -> None:
    result = parsed(make_tools(FakeInsightsClient(bytes_scanned=98765)).search_logs(VP_GROUP))

    assert result["bytesScanned"] == 98765


def test_an_empty_result_is_labelled_as_not_being_a_cause() -> None:
    """"Nothing matched" narrows where a cause can be; it is not one."""
    result = parsed(make_tools(FakeInsightsClient(rows=[])).search_logs(VP_GROUP))

    assert result["rowCount"] == 0
    assert "not a cause" in result["guidance"]


def test_a_non_empty_result_carries_no_guidance() -> None:
    client = FakeInsightsClient(rows=[row(m="VP exit: reason=completed")])

    assert parsed(make_tools(client).search_logs(VP_GROUP))["guidance"] is None


def test_a_whole_result_is_capped_so_one_call_cannot_fill_the_context() -> None:
    """A query can be cheap in bytes scanned and still return far too much."""
    limits = AgentLimits()
    client = FakeInsightsClient(
        rows=[row(message="y" * 1500) for _ in range(limits.max_log_events)]
    )

    result = make_tools(client, limits=limits).search_logs(VP_GROUP)

    assert len(result) <= limits.max_tool_result_chars + 100
    assert "truncated" in result


# ── search_logs: the query lifecycle ────────────────────────────────────────


def test_a_query_is_polled_until_it_completes() -> None:
    client = FakeInsightsClient(
        rows=[row(m="done")], statuses=["Scheduled", "Running", "Running", "Complete"]
    )

    result = parsed(make_tools(client).search_logs(VP_GROUP))

    assert result["status"] == "Complete"
    assert result["rows"] == [{"m": "done"}]


def test_a_query_that_outlasts_its_timeout_is_cancelled_not_abandoned() -> None:
    """An account has a bounded number of concurrent Insights queries, so
    abandoning one is how a runaway diagnosis starves the next."""
    client = FakeInsightsClient(statuses=["Running", "Running"])
    limits = AgentLimits()

    with pytest.raises(ToolError, match="was cancelled"):
        make_tools(client, limits=limits).search_logs(VP_GROUP)

    assert client.stopped == ["query-1"]


def test_a_cancelled_query_tells_the_agent_how_to_narrow_it() -> None:
    client = FakeInsightsClient(statuses=["Running", "Running"])

    with pytest.raises(ToolError, match="fewer log groups"):
        make_tools(client).search_logs(VP_GROUP)


def test_a_failed_query_is_reported_rather_than_read_as_empty() -> None:
    """"The query failed" and "nothing matched" lead to opposite conclusions."""
    client = FakeInsightsClient(statuses=["Failed"])

    with pytest.raises(ToolError, match="ended as Failed"):
        make_tools(client).search_logs(VP_GROUP)


def test_a_query_with_no_id_is_an_error_not_an_empty_result() -> None:
    class NoId(FakeInsightsClient):
        def start_query(self, **kwargs: Any) -> Dict[str, str]:
            return {}

    with pytest.raises(ToolError, match="did not return a query id"):
        make_tools(NoId()).search_logs(VP_GROUP)


def test_a_cancellation_that_itself_fails_does_not_mask_the_timeout() -> None:
    class StubbornQuery(FakeInsightsClient):
        def stop_query(self, queryId: str):  # noqa: N803
            raise RuntimeError("cannot stop")

    client = StubbornQuery(statuses=["Running", "Running"])

    with pytest.raises(ToolError, match="was cancelled"):
        make_tools(client).search_logs(VP_GROUP)


# ── the time budget ─────────────────────────────────────────────────────────


def test_a_tool_refuses_to_start_work_past_the_deadline() -> None:
    """One Insights query can outlast the whole budget by itself, so the
    deadline is checked here as well as in the agent loop."""
    clock = [100.0]

    for call in (
        lambda t: t.discover_log_groups(),
        lambda t: t.search_logs(VP_GROUP),
        lambda t: t.get_vp_record("vp-1"),
        lambda t: t.get_stack_parameters(),
    ):
        tools = make_tools(deadline=50.0, clock=clock)
        with pytest.raises(ToolError, match="time budget"):
            call(tools)


def test_the_deadline_message_tells_the_agent_to_name_the_missing_evidence() -> None:
    tools = make_tools(deadline=0.0, clock=[1.0])

    with pytest.raises(ToolError, match="evidence that is missing"):
        tools.discover_log_groups()


def test_work_proceeds_before_the_deadline() -> None:
    tools = make_tools(deadline=1000.0, clock=[0.0])

    assert parsed(tools.discover_log_groups())["logGroupCount"] > 0


# ── get_vp_record ───────────────────────────────────────────────────────────


class FakeDynamoDBClient:
    def __init__(self, item: Optional[Dict[str, Any]] = None):
        self.item = item
        self.calls: List[Dict[str, Any]] = []

    def get_item(self, **kwargs: Any) -> Dict[str, Any]:
        self.calls.append(kwargs)
        return {"Item": self.item} if self.item else {}


FAILED_VP_ITEM = {
    "id": {"S": "vp-abc"},
    "status": {"S": "FAILED"},
    "meetingPlatform": {"S": "TEAMS"},
    "errorMessage": {"S": "Could not find the name input"},
    "endTrigger": {"S": "pre-join:no-name-input"},
    "enableVideoRecording": {"BOOL": False},
    "vncPort": {"N": "5901"},
    "userAcknowledgedFailure": {"NULL": True},
}


def test_a_virtual_participant_record_is_read_by_its_id() -> None:
    client = FakeDynamoDBClient(FAILED_VP_ITEM)

    result = parsed(make_tools(dynamodb_client=client).get_vp_record("vp-abc"))

    assert result["found"] is True
    assert result["record"]["status"] == "FAILED"
    assert result["record"]["endTrigger"] == "pre-join:no-name-input"
    assert client.calls[0]["TableName"] == VP_TABLE
    assert client.calls[0]["Key"] == {"id": {"S": "vp-abc"}}


def test_the_record_is_read_consistently() -> None:
    """A diagnosis run seconds after a failure must not read a stale replica."""
    client = FakeDynamoDBClient(FAILED_VP_ITEM)

    make_tools(dynamodb_client=client).get_vp_record("vp-abc")

    assert client.calls[0]["ConsistentRead"] is True


def test_the_record_carries_the_caveat_that_error_message_is_overloaded() -> None:
    """errorMessage also carries JOINING progress, so a non-empty value does
    not by itself mean the virtual participant failed. A model without this
    reads the row wrongly in a specific and confident way."""
    result = parsed(
        make_tools(dynamodb_client=FakeDynamoDBClient(FAILED_VP_ITEM)).get_vp_record("vp-abc")
    )

    assert "JOINING sub-step progress" in result["fieldNotes"]["errorMessage"]


def test_the_record_says_that_end_trigger_is_only_written_on_terminal_states() -> None:
    result = parsed(
        make_tools(dynamodb_client=FakeDynamoDBClient(FAILED_VP_ITEM)).get_vp_record("vp-abc")
    )

    assert "COMPLETED and FAILED" in result["fieldNotes"]["endTrigger"]


def test_the_record_says_that_it_is_not_a_timeline() -> None:
    """The exit taxonomy and the admission verdicts are logs-only, so a model
    answering a "why" question from this row alone is answering from nothing."""
    result = parsed(
        make_tools(dynamodb_client=FakeDynamoDBClient(FAILED_VP_ITEM)).get_vp_record("vp-abc")
    )

    assert "not a timeline" in result["guidance"]
    assert "log group" in result["guidance"]


def test_a_note_is_only_attached_for_a_field_the_row_actually_has() -> None:
    client = FakeDynamoDBClient({"id": {"S": "vp-abc"}, "status": {"S": "JOINING"}})

    result = parsed(make_tools(dynamodb_client=client).get_vp_record("vp-abc"))

    assert set(result["fieldNotes"]) == {"status"}


def test_a_missing_row_is_not_reported_as_the_vp_never_having_run() -> None:
    """The row has a TTL; the launch state machine's log group outlives it."""
    result = parsed(
        make_tools(dynamodb_client=FakeDynamoDBClient(None)).get_vp_record("vp-gone")
    )

    assert result["found"] is False
    assert "not evidence that the virtual participant never ran" in result["guidance"]


def test_an_empty_virtual_participant_id_is_refused() -> None:
    for identifier in ("", "   ", None):
        with pytest.raises(ToolError, match="no virtual participant id"):
            make_tools(dynamodb_client=FakeDynamoDBClient()).get_vp_record(identifier)


def test_an_unconfigured_table_is_reported_rather_than_queried() -> None:
    client = FakeDynamoDBClient(FAILED_VP_ITEM)

    with pytest.raises(ToolError, match="table is not configured"):
        make_tools(dynamodb_client=client, vp_table_name="").get_vp_record("vp-abc")

    assert client.calls == []


# ── DynamoDB wire-format conversion ─────────────────────────────────────────


@pytest.mark.parametrize(
    ("attribute", "expected"),
    [
        ({"S": "text"}, "text"),
        ({"N": "42"}, 42),
        ({"N": "-7"}, -7),
        ({"N": "1.5"}, 1.5),
        ({"N": "1e3"}, 1000.0),
        ({"BOOL": True}, True),
        ({"BOOL": False}, False),
        ({"NULL": True}, None),
        ({"L": [{"S": "a"}, {"N": "1"}]}, ["a", 1]),
        ({"M": {"k": {"S": "v"}}}, {"k": "v"}),
        ({"SS": ["a", "b"]}, ["a", "b"]),
    ],
)
def test_a_wire_format_attribute_becomes_a_plain_value(
    attribute: Dict[str, Any], expected: Any
) -> None:
    assert _plain_value(attribute) == expected


def test_an_integer_valued_number_does_not_become_a_float() -> None:
    """A port or a poll count rendered as 5901.0 reads as a measurement."""
    assert _plain_value({"N": "5901"}) == 5901
    assert isinstance(_plain_value({"N": "5901"}), int)


def test_a_false_boolean_survives_conversion() -> None:
    """enableVideoRecording=false is a finding; dropping it loses the finding."""
    result = parsed(
        make_tools(dynamodb_client=FakeDynamoDBClient(FAILED_VP_ITEM)).get_vp_record("vp-abc")
    )

    assert result["record"]["enableVideoRecording"] is False
    assert result["record"]["userAcknowledgedFailure"] is None


def test_an_unrecognised_attribute_type_is_shown_rather_than_dropped() -> None:
    """An unexpected type should appear in the diagnosis, not vanish from it."""
    assert _plain_value({"XX": "surprise"}) == {"XX": "surprise"}
    assert _plain_value({"S": "a", "N": "1"}) == {"S": "a", "N": "1"}
    assert _plain_value("not an attribute") == "not an attribute"


# ── get_stack_parameters ────────────────────────────────────────────────────


class FakeCloudFormationClient:
    def __init__(self, stacks: Optional[List[Dict[str, Any]]] = None):
        self.stacks = stacks
        self.calls: List[str] = []

    def describe_stacks(self, StackName: str) -> Dict[str, Any]:  # noqa: N803
        self.calls.append(StackName)
        return {"Stacks": self.stacks if self.stacks is not None else []}


def stack_with(**parameters: str) -> List[Dict[str, Any]]:
    return [
        {
            "StackName": STACK,
            "StackStatus": "UPDATE_COMPLETE",
            "LastUpdatedTime": "2026-10-23T09:00:00Z",
            "Parameters": [
                {"ParameterKey": k, "ParameterValue": v} for k, v in parameters.items()
            ],
        }
    ]


def test_the_launch_type_is_reported_as_an_architecture_parameter() -> None:
    """Under MICROVM the Fargate log group is empty and no ECS task-stopped
    event is ever emitted, so an agent that has not read this reports "no
    evidence" for a deployment where the evidence is simply elsewhere."""
    client = FakeCloudFormationClient(
        stack_with(VPLaunchType="MICROVM", AdminEmail="a@example.com")
    )

    result = parsed(make_tools(cloudformation_client=client).get_stack_parameters())

    assert result["architectureParameters"]["VPLaunchType"] == "MICROVM"
    assert "permanently EMPTY" in result["architectureParameterNotes"]["VPLaunchType"] or (
        "Fargate log group is empty"
        in result["architectureParameterNotes"]["VPLaunchType"]
    )


def test_every_architecture_parameter_present_is_annotated() -> None:
    client = FakeCloudFormationClient(
        stack_with(
            VPLaunchType="FARGATE",
            EnableMicrovmAsr="true",
            MeetingAssistService="STRANDS_BEDROCK",
        )
    )

    result = parsed(make_tools(cloudformation_client=client).get_stack_parameters())

    assert set(result["architectureParameterNotes"]) == set(result["architectureParameters"])


def test_a_parameter_the_deployment_does_not_have_is_not_invented() -> None:
    client = FakeCloudFormationClient(stack_with(AdminEmail="a@example.com"))

    result = parsed(make_tools(cloudformation_client=client).get_stack_parameters())

    assert result["architectureParameters"] == {}
    assert result["architectureParameterNotes"] == {}


def test_the_remaining_parameters_are_reported_separately_and_sorted() -> None:
    client = FakeCloudFormationClient(
        stack_with(ZZZ="1", AAA="2", VPLaunchType="EC2")
    )

    result = parsed(make_tools(cloudformation_client=client).get_stack_parameters())

    assert list(result["otherParameters"]) == ["AAA", "ZZZ"]
    assert "VPLaunchType" not in result["otherParameters"]


def test_the_stack_status_and_update_time_are_reported() -> None:
    """A denial an hour after a deployment and one after a month of uptime are
    different findings, and only the stack's own timestamps distinguish them."""
    client = FakeCloudFormationClient(stack_with(VPLaunchType="MICROVM"))

    result = parsed(make_tools(cloudformation_client=client).get_stack_parameters())

    assert result["stackStatus"] == "UPDATE_COMPLETE"
    assert result["lastUpdatedTime"] == "2026-10-23T09:00:00Z"


def test_the_stack_that_is_described_is_the_lma_stack() -> None:
    client = FakeCloudFormationClient(stack_with())

    make_tools(cloudformation_client=client).get_stack_parameters()

    assert client.calls == [STACK]


def test_an_absent_stack_is_reported() -> None:
    with pytest.raises(ToolError, match="was not found"):
        make_tools(cloudformation_client=FakeCloudFormationClient([])).get_stack_parameters()


def test_an_unconfigured_stack_name_is_reported_rather_than_queried() -> None:
    client = FakeCloudFormationClient(stack_with())

    with pytest.raises(ToolError, match="stack name is not configured"):
        make_tools(cloudformation_client=client, stack_name="").get_stack_parameters()

    assert client.calls == []
