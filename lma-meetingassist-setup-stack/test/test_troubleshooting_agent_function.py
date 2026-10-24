#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""Unit tests for the troubleshooting agent's handler and guardrails (no AWS).

The guardrails are the point of this suite. They are the difference between a
diagnosis that costs cents and one that spends the whole Lambda timeout, and
an agentic loop has no natural stopping point, so each of the four bounds is
asserted here — the iteration cap, the token budget, the wall clock, and the
deliberate absence of a whole-run retry.

Asserting an absence is unusual and intentional. A retry is the obvious thing
for a later change to add, and it is the one control here whose cost is
invisible at the call site: it multiplies the spend of the run it repeats.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional
from unittest import mock

import pytest
from conftest import SOURCE_DIR

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("LMA_STACK_NAME", "LMA-Test")

# Clients are built at module scope, as the project's handlers all are.
with mock.patch("boto3.Session", mock.MagicMock()):
    import troubleshooting_agent_function as agent_function

from troubleshooting_config import AgentLimits


class Clock:
    """A monotonic clock a test can advance."""

    def __init__(self, now: float = 0.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def budget_with(clock: Optional[Clock] = None, **env: str) -> agent_function.RunBudget:
    environ = {f"TROUBLESHOOTING_{k.upper()}": v for k, v in env.items()}
    with mock.patch.dict("os.environ", environ, clear=False):
        limits = AgentLimits()
    return agent_function.RunBudget(limits, monotonic=clock or Clock())


# ── the iteration cap ───────────────────────────────────────────────────────


def test_a_run_within_the_iteration_cap_is_not_stopped() -> None:
    budget = budget_with(max_iterations="3")

    for _ in range(3):
        budget.note_tool_call()
        assert budget.exhausted_reason() is None


def test_the_call_after_the_cap_is_stopped() -> None:
    """Without this cap the only bound on a loop that keeps calling tools is
    the Lambda timeout, which is a budget of seconds rather than of calls."""
    budget = budget_with(max_iterations="3")

    for _ in range(4):
        budget.note_tool_call()

    assert "3-tool-call limit" in (budget.exhausted_reason() or "")


def test_the_iteration_cap_is_configurable() -> None:
    budget = budget_with(max_iterations="1")
    budget.note_tool_call()
    budget.note_tool_call()

    assert budget.exhausted_reason() is not None


# ── the token budget ────────────────────────────────────────────────────────


def test_a_run_inside_the_token_budget_continues() -> None:
    budget = budget_with(token_budget="1000")
    budget.note_usage(400, 300)

    assert budget.exhausted_reason() is None


def test_a_run_over_the_token_budget_is_stopped() -> None:
    """The system prompt is large and resent on every turn, which is what makes
    a per-turn output cap insufficient on its own."""
    budget = budget_with(token_budget="1000")
    budget.note_usage(900, 200)

    assert "1000-token budget" in (budget.exhausted_reason() or "")


def test_usage_is_treated_as_a_cumulative_total_not_a_delta() -> None:
    """The SDK reports accumulated usage, so taking these as deltas would
    double-count and stop a run that was well inside its budget."""
    budget = budget_with(token_budget="100000")
    budget.note_usage(1000, 100)
    budget.note_usage(1800, 250)

    assert (budget.input_tokens, budget.output_tokens) == (1800, 250)
    assert budget.total_tokens == 2050


def test_a_usage_report_that_goes_backwards_does_not_lower_the_count() -> None:
    """A counter reset mid-run must not hand the agent back its budget."""
    budget = budget_with()
    budget.note_usage(5000, 500)
    budget.note_usage(10, 1)

    assert budget.total_tokens == 5500


@pytest.mark.parametrize("value", [None, 0, "", False])
def test_an_absent_usage_figure_is_counted_as_zero_not_an_error(value: Any) -> None:
    budget = budget_with()
    budget.note_usage(value, value)

    assert budget.total_tokens == 0


# ── the wall clock ──────────────────────────────────────────────────────────


def test_the_deadline_is_the_start_plus_the_configured_budget() -> None:
    clock = Clock(1000.0)
    budget = budget_with(clock, wall_clock_seconds="90")

    assert budget.deadline == 1090.0


def test_a_run_past_its_deadline_is_stopped_even_with_budget_left() -> None:
    """Neither the iteration cap nor the token budget can bound a run on its
    own: a single Logs Insights query can legitimately take 45 seconds, and a
    stalled HTTP call burns time without burning tokens."""
    clock = Clock()
    budget = budget_with(clock, wall_clock_seconds="60", max_iterations="100")
    budget.note_tool_call()
    assert budget.exhausted_reason() is None

    clock.advance(61)

    assert "60-second time budget" in (budget.exhausted_reason() or "")


def test_the_deadline_is_handed_to_the_tools_as_well_as_the_loop() -> None:
    """The tools check it too, because a query already in flight when the
    budget expires has to be cancelled rather than waited on."""
    clock = Clock(500.0)
    budget = budget_with(clock, wall_clock_seconds="120")

    assert budget.deadline == 620.0


def test_elapsed_time_is_reported() -> None:
    clock = Clock()
    budget = budget_with(clock)
    clock.advance(12.34)

    assert budget.as_dict()["elapsedSeconds"] == 12.3


# ── the wall clock cannot outrun the invocation ─────────────────────────────
#
# The configured budget is a request. The function's Timeout is a template
# value an operator can raise or lower, so a budget written in configuration
# cannot be guaranteed to sit inside it; the invocation's own remaining time is
# what actually bounds the run.


def budget_for(remaining: float, clock: Clock, **env: str) -> agent_function.RunBudget:
    environ = {f"TROUBLESHOOTING_{k.upper()}": v for k, v in env.items()}
    with mock.patch.dict("os.environ", environ, clear=False):
        limits = AgentLimits()
    return agent_function.RunBudget(limits, monotonic=clock, remaining_seconds=remaining)


def test_a_budget_longer_than_the_invocation_is_cut_to_fit() -> None:
    """Otherwise the deadline is one the run can never reach, and the Lambda is
    killed mid-answer instead of the agent reporting that it ran out of time."""
    clock = Clock()
    budget = budget_for(
        100.0, clock, wall_clock_seconds="600", answer_reserve_seconds="30"
    )

    assert budget.wall_clock_seconds == 70.0
    assert budget.capped_by_invocation is True


def test_a_budget_inside_the_invocation_is_left_alone() -> None:
    clock = Clock()
    budget = budget_for(
        600.0, clock, wall_clock_seconds="240", answer_reserve_seconds="30"
    )

    assert budget.wall_clock_seconds == 240.0
    assert budget.capped_by_invocation is False


def test_time_is_reserved_for_the_model_to_write_its_answer() -> None:
    """Evidence gathering stopping is not the end of the run: a full model turn
    happens after the last tool returns."""
    clock = Clock()
    budget = budget_for(
        200.0, clock, wall_clock_seconds="900", answer_reserve_seconds="45"
    )

    assert budget.wall_clock_seconds == 155.0


def test_an_invocation_with_no_time_left_still_produces_a_reason() -> None:
    """A deadline already past makes the first tool refuse, which yields "ran
    out of time" rather than an empty answer with no explanation."""
    clock = Clock()
    budget = budget_for(1.0, clock, answer_reserve_seconds="30")

    assert budget.wall_clock_seconds == 1.0
    clock.advance(2)
    assert "time budget" in (budget.exhausted_reason() or "")


def test_an_unknown_remaining_time_falls_back_to_the_configured_budget() -> None:
    clock = Clock()
    budget = budget_for(None, clock, wall_clock_seconds="240")

    assert budget.wall_clock_seconds == 240.0
    assert budget.capped_by_invocation is False


def test_the_effective_budget_is_reported_not_the_requested_one() -> None:
    clock = Clock()
    budget = budget_for(
        100.0, clock, wall_clock_seconds="600", answer_reserve_seconds="30"
    )

    reported = budget.as_dict()
    assert reported["wallClockSeconds"] == 70.0
    assert reported["wallClockCappedByInvocation"] is True


def test_the_stop_reason_quotes_the_effective_budget() -> None:
    """Quoting the configured 600 while stopping at 70 would make the limit
    look broken."""
    clock = Clock()
    budget = budget_for(
        100.0, clock, wall_clock_seconds="600", answer_reserve_seconds="30"
    )
    clock.advance(71)

    assert "70-second time budget" in (budget.exhausted_reason() or "")


# ── reading the invocation's remaining time ─────────────────────────────────


def test_the_remaining_time_is_read_from_the_lambda_context() -> None:
    context = mock.Mock()
    context.get_remaining_time_in_millis.return_value = 285_000

    assert agent_function.remaining_seconds(context) == 285.0


@pytest.mark.parametrize("context", [None, object(), "not a context"])
def test_a_context_that_cannot_report_its_time_yields_nothing(context: Any) -> None:
    assert agent_function.remaining_seconds(context) is None


def test_a_context_whose_clock_raises_yields_nothing() -> None:
    """An unreadable clock costs the cap its precision, not the run."""
    context = mock.Mock()
    context.get_remaining_time_in_millis.side_effect = RuntimeError("no clock")

    assert agent_function.remaining_seconds(context) is None


# ── there is no whole-run retry ─────────────────────────────────────────────


def test_the_handler_does_not_retry_a_failed_run() -> None:
    """Re-running a diagnosis from the original question re-pays for every tool
    call it already made, for a question whose answer has not changed. A failed
    run is reported once, and asking again is the operator's decision.
    """
    attempts = []

    def failing(event: Dict[str, Any], context: Any = None) -> Dict[str, Any]:
        attempts.append(event)
        raise RuntimeError("bedrock said no")

    with mock.patch.object(agent_function, "run_diagnosis", failing):
        response = agent_function.handler({"query": "why?"}, None)

    assert len(attempts) == 1
    assert response["statusCode"] == 500
    assert "bedrock said no" in json.loads(response["body"])["error"]


def test_no_retry_knob_is_offered_anywhere() -> None:
    """Making the retry configurable would be the first step to reintroducing
    it, so the limits table deliberately has no entry for one."""
    assert not [name for name in AgentLimits().as_dict() if "retr" in name.lower()]


# ── the handler's contract ──────────────────────────────────────────────────


def test_a_successful_run_returns_the_answer_and_the_run_metadata() -> None:
    with mock.patch.object(
        agent_function,
        "run_diagnosis",
        lambda event, context=None: {
            "answer": "## Root Cause\nX",
            "usage": {"toolCalls": 3},
        },
    ):
        response = agent_function.handler({"query": "why?"}, None)

    body = json.loads(response["body"])
    assert response["statusCode"] == 200
    assert body["answer"].startswith("## Root Cause")
    assert body["usage"]["toolCalls"] == 3


def test_an_unavailable_prompt_is_a_configuration_error_not_a_crash() -> None:
    """A diagnosis produced from an unknown prompt is worse than none, so this
    path refuses rather than substituting a prompt."""

    def unavailable(event: Dict[str, Any], context: Any = None) -> Dict[str, Any]:
        raise agent_function.troubleshooting_prompt.PromptUnavailableError("no item")

    with mock.patch.object(agent_function, "run_diagnosis", unavailable):
        response = agent_function.handler({}, None)

    assert response["statusCode"] == 400
    assert "system prompt is not available" in json.loads(response["body"])["error"]


def test_an_empty_event_is_accepted() -> None:
    with mock.patch.object(
        agent_function, "run_diagnosis", lambda event, context=None: {"answer": "x"}
    ):
        assert agent_function.handler(None, None)["statusCode"] == 200


# ── the question handed to the model ────────────────────────────────────────


def test_the_question_restates_the_identifiers_as_facts() -> None:
    """The agent has to pass these to a tool verbatim, and a model extracting
    an id out of a sentence sometimes extracts it wrongly."""
    question = agent_function.build_question(
        {"query": "why did it fail?", "vpId": "vp-9", "callId": "call-7"}
    )

    assert "QUESTION: why did it fail?" in question
    assert "Virtual participant id in scope: vp-9" in question
    assert "Meeting id in scope: call-7" in question


def test_the_question_names_the_stack_under_diagnosis() -> None:
    assert "LMA stack:" in agent_function.build_question({"query": "x"})


def test_the_question_states_the_current_time() -> None:
    """Almost every pattern in the prompt is "did this start recently", which
    the model cannot judge without knowing when now is."""
    assert "Current time (UTC):" in agent_function.build_question({})


def test_an_absent_query_still_asks_something_answerable() -> None:
    assert "most recent failure" in agent_function.build_question({})


@pytest.mark.parametrize("query", ["", "   ", None])
def test_a_blank_query_falls_back_rather_than_asking_nothing(query: Any) -> None:
    assert "most recent failure" in agent_function.build_question({"query": query})


def test_an_identifier_that_was_not_supplied_is_not_mentioned() -> None:
    question = agent_function.build_question({"query": "x"})

    assert "in scope" not in question


# ── the guardrail configuration ─────────────────────────────────────────────


def test_no_guardrail_is_applied_when_none_is_configured() -> None:
    """An empty dict spread into the model configuration leaves the request
    exactly as it would have been, which is the default deployment."""
    with mock.patch.multiple(
        agent_function, BEDROCK_GUARDRAIL_ID="", BEDROCK_GUARDRAIL_VERSION=""
    ):
        assert agent_function.get_guardrail_config() == {}


def test_a_fully_configured_guardrail_is_applied() -> None:
    with mock.patch.multiple(
        agent_function, BEDROCK_GUARDRAIL_ID="abc123", BEDROCK_GUARDRAIL_VERSION="DRAFT"
    ):
        assert agent_function.get_guardrail_config() == {
            "guardrail_id": "abc123",
            "guardrail_version": "DRAFT",
        }


@pytest.mark.parametrize(
    ("guardrail_id", "version"), [("abc123", ""), ("", "DRAFT")]
)
def test_a_half_configured_guardrail_applies_none(guardrail_id: str, version: str) -> None:
    """A guardrail is only usable with both halves. Applying one half would
    build a malformed request that fails every turn; the IAM grant for it is
    omitted on the same condition."""
    with mock.patch.multiple(
        agent_function,
        BEDROCK_GUARDRAIL_ID=guardrail_id,
        BEDROCK_GUARDRAIL_VERSION=version,
    ):
        assert agent_function.get_guardrail_config() == {}


def test_diagnosis_is_deterministic_where_the_model_allows_it() -> None:
    """Diagnosis is extractive: the same evidence should yield the same cause."""
    assert agent_function.AGENT_TEMPERATURE == 0.0


def test_bedrock_gets_the_projects_aggressive_retry_profile() -> None:
    """BedrockModel builds its own client, so a config left on the module is
    simply not used and the model runs on the SDK's defaults. Cross-region
    inference profiles make a first attempt fail often enough that this is the
    difference between a working deployment and an intermittent one."""
    config = agent_function.BEDROCK_CONFIG

    assert config.retries["max_attempts"] == 50
    assert config.retries["mode"] == "adaptive"


def test_the_retry_profile_actually_reaches_the_model() -> None:
    """The assertion the previous test cannot make on its own: the config has
    to be passed, not merely defined."""
    source = (SOURCE_DIR / "troubleshooting_agent_function.py").read_text(
        encoding="utf-8"
    )

    assert '"boto_client_config": BEDROCK_CONFIG' in source


# ── what the thinking-step stream publishes ─────────────────────────────────
#
# This channel reaches the UI without the model in the loop, so everything the
# prompt says about what to quote is bypassed here. search_logs returns raw log
# lines, and a log line contains whatever the component that wrote it put
# there, so a prefix of the raw result is the one thing this must not publish.


def test_a_log_search_result_is_published_as_its_shape_not_its_content() -> None:
    result = json.dumps(
        {
            "rowCount": 42,
            "bytesScanned": 90000,
            "rows": [{"@message": "Authorization: Bearer super-secret-token"}],
        }
    )

    summary = agent_function.ThinkingStepHookProvider.summarise_result(result)

    assert "42 rows" in summary
    assert "super-secret-token" not in summary
    assert "Bearer" not in summary


def test_a_discovery_result_is_published_as_a_count() -> None:
    summary = agent_function.ThinkingStepHookProvider.summarise_result(
        json.dumps({"logGroupCount": 19, "logGroups": [{"logGroupName": "/x"}]})
    )

    assert "19 log groups" in summary
    assert "/x" not in summary


def test_a_record_result_says_only_whether_it_was_found() -> None:
    found = agent_function.ThinkingStepHookProvider.summarise_result(
        json.dumps({"found": True, "record": {"meetingName": "Board review"}})
    )
    missing = agent_function.ThinkingStepHookProvider.summarise_result(
        json.dumps({"found": False})
    )

    assert found == "record found"
    assert "Board review" not in found
    assert missing == "no record"


def test_a_tool_error_publishes_its_label_and_not_its_message() -> None:
    """Some of those messages embed a botocore exception's text or a log group
    name the model supplied, so the label written at the raise site is what
    reaches the stream."""
    summary = agent_function.ThinkingStepHookProvider.summarise_result(
        json.dumps(
            {
                "error": "none of the named log groups exist: "
                "['/aws/lambda/invented-by-the-model']",
                "errorSummary": "named log groups do not exist in this deployment",
                "tool": "search_logs",
            }
        )
    )

    assert "named log groups do not exist in this deployment" in summary
    assert "invented-by-the-model" not in summary


def test_a_tool_error_with_no_label_still_says_what_kind_of_failure_it_was() -> None:
    summary = agent_function.ThinkingStepHookProvider.summarise_result(
        json.dumps({"error": "something with an arn:aws: in it"})
    )

    assert summary == "could not gather evidence"
    assert "arn:aws:" not in summary


def test_a_refused_call_is_published_as_a_refusal_not_a_success() -> None:
    """A refusal returns normally, so the SDK reports it as a success. Only
    the payload knows otherwise, and a progress panel showing a refused call
    as a completed one is actively misleading."""
    refusal = json.dumps(
        {"stopped": "the 8-tool-call limit has been reached", "instruction": "..."}
    )
    provider = agent_function.ThinkingStepHookProvider

    assert "refused" in provider.summarise_result(refusal)
    assert "8-tool-call limit" in provider.summarise_result(refusal)
    assert provider._result_succeeded({"content": [{"text": refusal}]}) is False


def test_a_tool_fault_publishes_its_type_and_not_its_message() -> None:
    """botocore error text can carry account-specific ARNs, and this stream
    reaches the UI without the model in the loop. The full message still goes
    to the model and to the log."""
    fault = json.dumps(
        {
            "error": "ClientError: User arn:aws:sts::111122223333:assumed-role/x is "
            "not authorized",
            "fault": "ClientError",
            "tool": "search_logs",
        }
    )
    provider = agent_function.ThinkingStepHookProvider

    summary = provider.summarise_result(fault)
    assert summary == "failed: ClientError"
    assert "111122223333" not in summary
    assert provider._result_succeeded({"content": [{"text": fault}]}) is False


def test_a_tool_error_is_reported_as_an_unsuccessful_call() -> None:
    failure = json.dumps(
        {
            "error": "none of the named log groups exist",
            "errorSummary": "named log groups do not exist in this deployment",
            "tool": "search_logs",
        }
    )
    provider = agent_function.ThinkingStepHookProvider

    assert provider._result_succeeded({"content": [{"text": failure}]}) is False


def test_a_successful_result_is_still_reported_as_a_success() -> None:
    body = json.dumps({"rowCount": 3, "logGroupCount": 1})

    assert (
        agent_function.ThinkingStepHookProvider._result_succeeded(
            {"content": [{"text": body}]}
        )
        is True
    )


@pytest.mark.parametrize(
    "content",
    [
        "not json at all",
        "[1, 2, 3]",
        "",
        None,
        '{"unexpected": "shape"}',
    ],
)
def test_an_unrecognised_result_publishes_nothing_about_itself(content: Any) -> None:
    """The failure direction that matters: an unparsed result must not fall
    back to printing itself."""
    summary = agent_function.ThinkingStepHookProvider.summarise_result(content)

    assert summary == "completed"


# ── the hooks against the SDK's real event shapes ───────────────────────────
#
# The Strands Agents SDK models a tool use and a tool result as TypedDicts --
# plain dicts at runtime -- while the hook events carrying them are objects.
# So the event needs attribute access and its payload needs key access, and a
# reader that assumes attributes everywhere silently gets None from the
# payload: the summary would always say "completed" and every call would report
# success. These drive the hooks with the dict shapes rather than testing the
# summariser in isolation.


def published_steps(event: Any, after: bool = True) -> List[Dict[str, Any]]:
    """Run a hook against `event` and return the thinking steps it published."""
    captured: List[Dict[str, Any]] = []
    provider = agent_function.ThinkingStepHookProvider(
        budget_with(), call_id="call-1", message_id="message-1"
    )
    with mock.patch.object(
        agent_function,
        "send_thinking_step",
        lambda call_id, message_id, step, sequence: captured.append(step),
    ):
        if after:
            provider.on_after_tool_call(event)
        else:
            provider.on_before_tool_call(event)
    return captured


def tool_result_event(payload: Dict[str, Any], status: str = "success") -> Any:
    """An AfterToolCallEvent as the SDK builds it: object, with a dict result."""
    event = mock.Mock()
    event.selected_tool = mock.Mock(tool_name="search_logs")
    # ToolResult is a TypedDict: content is a list of blocks carrying `text`.
    event.result = {
        "status": status,
        "toolUseId": "tu-1",
        "content": [{"text": json.dumps(payload)}],
    }
    event.agent = None
    return event


def test_a_dict_shaped_tool_result_is_read_and_summarised() -> None:
    """A ToolResult is a TypedDict, so the text has to be read out of the
    dict's `content` blocks. Reading it as an attribute yields None, and the
    summary then describes nothing."""
    steps = published_steps(tool_result_event({"rowCount": 42, "logGroupCount": 2}))

    assert len(steps) == 1
    assert "42 rows" in steps[0]["result"]
    assert "2 log groups" in steps[0]["result"]
    assert steps[0]["tool_name"] == "search_logs"


def test_a_tool_result_split_across_content_blocks_is_joined() -> None:
    event = tool_result_event({})
    body = json.dumps({"rowCount": 7})
    event.result["content"] = [{"text": body[:4]}, {"text": body[4:]}]

    assert "7 rows" in published_steps(event)[0]["result"]


def test_a_json_content_block_is_read_too() -> None:
    event = tool_result_event({})
    event.result["content"] = [{"json": {"logGroupCount": 11}}]

    assert "11 log groups" in published_steps(event)[0]["result"]


def test_an_error_status_is_reported_as_a_failed_call() -> None:
    """Success comes from ToolResult's `status` key. Read as an attribute it is
    always absent, which would make every call look successful."""
    steps = published_steps(tool_result_event({"rowCount": 0}, status="error"))

    assert steps[0]["success"] is False


def test_a_success_status_is_reported_as_a_successful_call() -> None:
    assert published_steps(tool_result_event({"rowCount": 1}))[0]["success"] is True


def test_an_unknown_status_counts_as_success() -> None:
    """Narration that reported every call as failed would be worse than one
    that reported none."""
    event = tool_result_event({"rowCount": 1})
    del event.result["status"]

    assert published_steps(event)[0]["success"] is True


def test_an_older_attribute_shaped_result_still_works() -> None:
    """Belt and braces: the reader tries both, so neither shape is a cliff."""
    event = mock.Mock()
    event.selected_tool = mock.Mock(tool_name="search_logs")
    event.result = mock.Mock(
        content=json.dumps({"rowCount": 5}), is_error=False, status=None
    )
    event.agent = None

    steps = published_steps(event)

    assert "5 rows" in steps[0]["result"]
    assert steps[0]["success"] is True


def test_a_dict_shaped_tool_use_is_read_and_summarised() -> None:
    """A ToolUse is a TypedDict too, so the arguments have to be read by key
    for the progress panel to show what the agent asked for."""
    event = mock.Mock()
    event.selected_tool = mock.Mock(tool_name="search_logs")
    event.tool_use = {
        "toolUseId": "tu-1",
        "name": "search_logs",
        "input": {"log_group_names": "/a/b", "lookback_hours": 6},
    }

    steps = published_steps(event, after=False)

    assert "1 log group" in steps[0]["tool_input_summary"]
    assert "lookback_hours=6" in steps[0]["tool_input_summary"]


def summarise_input(tool_input: Any, vp_id: str = "") -> Optional[str]:
    """Summarise a tool call's arguments as the before-tool hook would.

    `vp_id` is the identifier the invocation supplied, which is what decides
    whether a vp_id argument is this function's value to publish or the
    model's.
    """
    provider = agent_function.ThinkingStepHookProvider(budget_with(), vp_id=vp_id)
    return provider.summarise_input(tool_input)


def test_a_model_authored_query_is_not_published_verbatim() -> None:
    """The input is written *by* the model, often from text it has just read
    out of a log line, so publishing it would put log content on this stream
    by a side door — which is what summarising the result prevents."""
    summary = summarise_input(
        {
            "log_group_names": "/a/b",
            "query_string": 'fields @message | filter @message like "sk_live_SECRET"',
        }
    )

    assert "sk_live_SECRET" not in summary
    assert "query_string (" in summary
    assert "chars)" in summary


def test_search_terms_are_not_published_verbatim_either() -> None:
    summary = summarise_input(
        {"log_group_names": "/a/b", "search_terms": "Bearer abc.def.ghi"}
    )

    assert "Bearer" not in summary
    assert "search_terms (" in summary


def test_log_group_names_are_counted_and_classified_not_echoed() -> None:
    """A name reaching the before-tool hook has not been checked against
    discovery yet, so it is whatever the model wrote. The component it
    classifies as comes from a closed set, and is the more useful thing for a
    progress panel anyway.
    """
    summary = summarise_input(
        {"log_group_names": "/aws/lambda-microvms/LMA-vp-logs"}
    )
    assert "1 log group" in summary
    assert "in virtual-participant" in summary
    assert "LMA-vp-logs" not in summary


def test_an_invented_log_group_name_does_not_reach_the_stream() -> None:
    summary = summarise_input(
        {"log_group_names": "/aws/lambda/secret-sounding-name-the-model-made-up"}
    )

    assert "secret-sounding-name" not in summary
    assert "1 log group" in summary


def test_several_log_groups_are_counted_and_their_components_listed() -> None:
    summary = summarise_input(
        {
            "log_group_names": "/aws/lambda-microvms/LMA-vp-logs,"
            "/aws/vendedlogs/states/LMA-VPSTACK-x,"
            "/LMA-AISTACK-y/lambda/CallEventProcessorFunction"
        }
    )

    assert "3 log groups" in summary
    assert "virtual-participant" in summary
    assert "transcript-pipeline" in summary


def test_the_components_argument_is_not_published_by_value() -> None:
    """It is free text the model wrote, like search_terms."""
    summary = summarise_input(
        {"components": "virtual-participant,something-made-up"}
    )

    assert "something-made-up" not in summary
    assert "components (" in summary


def test_numbers_the_agent_chose_are_published() -> None:
    """A number cannot carry log content whatever the model puts in it."""
    summary = summarise_input({"lookback_hours": 12, "limit": 50})

    assert "lookback_hours=12" in summary
    assert "limit=50" in summary


def test_the_scoped_virtual_participant_id_is_published() -> None:
    """When the agent passes back the identifier the invocation gave it, the
    value is this function's own and there is nothing to withhold."""
    summary = summarise_input({"vp_id": "vp-abc"}, vp_id="vp-abc")

    assert "vp_id=vp-abc" in summary


def test_a_virtual_participant_id_the_model_invented_is_not_published() -> None:
    """Any other value is model-written, and goes the way the other
    model-written arguments do."""
    summary = summarise_input({"vp_id": "vp-something-else"}, vp_id="vp-abc")

    assert "vp-something-else" not in summary
    assert "vp_id (" in summary


def test_a_virtual_participant_id_with_no_scope_to_compare_is_not_published() -> None:
    """A stack-wide question supplies no vpId, so there is nothing that makes
    the argument anything but the model's."""
    summary = summarise_input({"vp_id": "vp-abc"})

    assert "vp-abc" not in summary
    assert "vp_id (" in summary


@pytest.mark.parametrize("tool_input", [None, {}, "not a mapping", 7, []])
def test_an_input_with_nothing_to_say_publishes_nothing(tool_input: Any) -> None:
    assert summarise_input(tool_input) is None


def test_an_unrecognised_argument_is_not_published() -> None:
    """The allowlist's failure direction: a tool argument added later is left
    off the stream until somebody decides it is safe to publish."""
    summary = summarise_input(
        {"log_group_names": "/a/b", "some_future_argument": "sensitive"}
    )

    assert "sensitive" not in summary
    assert "some_future_argument" not in summary


def test_the_tool_name_is_found_on_the_tool_use_when_there_is_no_selected_tool() -> None:
    event = mock.Mock(spec=["tool_use"])
    event.tool_use = {"name": "get_vp_record", "input": {}}

    assert published_steps(event, after=False)[0]["tool_name"] == "get_vp_record"


def test_a_hook_failure_never_ends_a_diagnosis() -> None:
    """An observability failure must not cost the answer."""
    provider = agent_function.ThinkingStepHookProvider(budget_with())

    provider.on_after_tool_call(object())
    provider.on_before_tool_call(object())


# ── narrating the investigation to the UI ───────────────────────────────────


def send_steps(count: int, **env: str) -> Any:
    """Publish `count` thinking steps against a stubbed AppSync client."""
    client = mock.MagicMock()
    environ = {"APPSYNC_GRAPHQL_URL": "https://example.invalid/graphql"}
    environ.update(env)
    with mock.patch.multiple(
        agent_function,
        APPSYNC_GRAPHQL_URL=environ["APPSYNC_GRAPHQL_URL"],
        _APPSYNC_CLIENT=None,
        _PARSED_MUTATION=None,
    ), mock.patch.object(
        agent_function, "_appsync_publisher", return_value=(client, "mutation")
    ):
        for sequence in range(count):
            agent_function.send_thinking_step(
                "call-1", "message-1", {"type": "tool_use", "tool_name": "x"}, sequence
            )
    return client


def test_a_thinking_step_is_published_on_the_meeting_s_channel() -> None:
    client = send_steps(1)

    variables = client.execute.call_args.kwargs["variable_values"]["input"]
    assert variables["CallId"] == "call-1"
    assert variables["MessageId"] == "message-1"
    assert variables["ThinkingStep"]["ToolName"] == "x"


def test_a_thinking_step_carries_an_empty_token() -> None:
    """It rides the token stream, so the UI must not render it as an answer."""
    client = send_steps(1)

    variables = client.execute.call_args.kwargs["variable_values"]["input"]
    assert variables["Token"] == ""
    assert variables["IsComplete"] is False


def test_the_appsync_client_is_reused_across_steps() -> None:
    """A diagnosis publishes two steps per tool call; building a fresh SigV4
    client each time would pay for a connection setup per step."""
    client = send_steps(6)

    assert client.execute.call_count == 6


def test_the_cached_client_is_not_rebuilt_once_set() -> None:
    sentinel = mock.MagicMock()
    with mock.patch.multiple(
        agent_function,
        APPSYNC_GRAPHQL_URL="https://example.invalid/graphql",
        _APPSYNC_CLIENT=sentinel,
        _PARSED_MUTATION="mutation",
    ):
        client, mutation = agent_function._appsync_publisher()

    assert client is sentinel
    assert mutation == "mutation"


@pytest.mark.parametrize(
    ("url", "call_id", "message_id"),
    [
        ("", "call-1", "message-1"),
        ("https://example.invalid/graphql", "", "message-1"),
        ("https://example.invalid/graphql", "call-1", ""),
    ],
)
def test_nothing_is_published_without_somewhere_to_publish_it(
    url: str, call_id: str, message_id: str
) -> None:
    """onAddChatToken needs the message id, so a caller that supplied none
    cannot be subscribed and the mutation would persist nothing."""
    client = mock.MagicMock()
    with mock.patch.multiple(agent_function, APPSYNC_GRAPHQL_URL=url), mock.patch.object(
        agent_function, "_appsync_publisher", return_value=(client, "mutation")
    ):
        agent_function.send_thinking_step(call_id, message_id, {"type": "tool_use"}, 0)

    client.execute.assert_not_called()


def test_a_publishing_failure_does_not_end_the_diagnosis() -> None:
    """A run that completed but could not narrate itself is still a run."""
    client = mock.MagicMock()
    client.execute.side_effect = RuntimeError("appsync unreachable")

    with mock.patch.multiple(
        agent_function, APPSYNC_GRAPHQL_URL="https://example.invalid/graphql"
    ), mock.patch.object(
        agent_function, "_appsync_publisher", return_value=(client, "mutation")
    ):
        agent_function.send_thinking_step("c", "m", {"type": "tool_use"}, 0)


# ── the tool wrapper ────────────────────────────────────────────────────────
#
# build_tools needs the Strands layer, which is absent here. The wrapper's
# behaviour is what matters, so it is exercised through a stubbed decorator.


class FakeTools:
    """Stands in for TroubleshootingTools, recording what was asked of it."""

    def __init__(self, raises: Optional[Exception] = None):
        self.calls: List[str] = []
        self.raises = raises

    def _record(self, name: str, **kwargs: Any) -> str:
        self.calls.append(name)
        if self.raises:
            raise self.raises
        return json.dumps({"ok": name, "args": sorted(kwargs)})

    def discover_log_groups(self, **kwargs: Any) -> str:
        return self._record("discover_log_groups", **kwargs)

    def search_logs(self, **kwargs: Any) -> str:
        return self._record("search_logs", **kwargs)

    def get_vp_record(self, **kwargs: Any) -> str:
        return self._record("get_vp_record", **kwargs)

    def get_stack_parameters(self, **kwargs: Any) -> str:
        return self._record("get_stack_parameters", **kwargs)


def build_tools(tools: Any, budget: agent_function.RunBudget) -> Dict[str, Any]:
    """Call build_tools with `strands.tool` stubbed out, keyed by tool name."""
    strands = mock.MagicMock()
    strands.tool = lambda fn: fn
    with mock.patch.dict("sys.modules", {"strands": strands}):
        return {fn.__name__: fn for fn in agent_function.build_tools(tools, budget)}


def test_all_four_tools_are_registered() -> None:
    assert set(build_tools(FakeTools(), budget_with())) == {
        "discover_log_groups",
        "search_logs",
        "get_vp_record",
        "get_stack_parameters",
    }


def test_the_stack_parameters_tool_is_offered_first() -> None:
    """Ordering is a nudge, not a control -- the prompt is what instructs the
    agent to read the parameters before concluding evidence is absent -- but
    the tool a model reaches for first is correlated with the order it sees."""
    names = list(build_tools(FakeTools(), budget_with()))

    assert names[0] == "get_stack_parameters"


def test_every_tool_has_a_docstring_for_the_model_to_route_on() -> None:
    """With Strands the docstring IS the tool description the model sees."""
    for name, fn in build_tools(FakeTools(), budget_with()).items():
        assert fn.__doc__ and len(fn.__doc__) > 100, name


def test_a_tool_call_reaches_the_implementation() -> None:
    tools = FakeTools()

    build_tools(tools, budget_with())["get_vp_record"]("vp-1")

    assert tools.calls == ["get_vp_record"]


def test_each_tool_call_is_counted_by_the_wrapper_not_by_a_hook() -> None:
    """Counting in the wrapper is what makes the cap hold whether or not the
    SDK's hook events fire with the shape this version expects."""
    budget = budget_with(max_iterations="10")
    tools = build_tools(FakeTools(), budget)

    tools["get_stack_parameters"]()
    tools["discover_log_groups"]()

    assert budget.tool_calls == 2


def test_a_tool_past_the_cap_returns_a_stop_instruction_and_does_no_work() -> None:
    """Refusing with an instruction rather than an exception ends the run with
    an answer: the model is told to write up what it already has."""
    budget = budget_with(max_iterations="1")
    impl = FakeTools()
    tools = build_tools(impl, budget)

    tools["get_stack_parameters"]()
    refused = json.loads(tools["discover_log_groups"]())

    assert impl.calls == ["get_stack_parameters"]
    assert "1-tool-call limit" in refused["stopped"]
    assert "STOP GATHERING EVIDENCE" in refused["instruction"]
    assert "Not determined" in refused["instruction"]
    assert budget.stopped_because is not None


def test_a_stopped_run_is_reported_as_truncated() -> None:
    """The caller needs to know the answer was written under a cut-short
    investigation, so the reason is carried out with the result."""
    budget = budget_with(max_iterations="1")
    tools = build_tools(FakeTools(), budget)

    tools["get_stack_parameters"]()
    assert budget.as_dict()["stoppedBecause"] is None

    tools["discover_log_groups"]()

    assert "1-tool-call limit" in budget.as_dict()["stoppedBecause"]


# ── a reached limit ends the loop, it does not just ask ─────────────────────


def test_a_second_refused_call_ends_the_run_instead_of_asking_again() -> None:
    """The stop instruction is a request to a model, and a request is not a
    limit: a model that calls another tool anyway buys a full turn, which
    re-sends the whole system prompt. The second refusal unwinds the loop."""
    budget = budget_with(max_iterations="1")
    impl = FakeTools()
    tools = build_tools(impl, budget)

    tools["get_stack_parameters"]()
    first_refusal = json.loads(tools["discover_log_groups"]())
    assert "STOP GATHERING EVIDENCE" in first_refusal["instruction"]

    with pytest.raises(agent_function.RunBudgetExhausted):
        tools["search_logs"]("whatever")

    # No tool did any work after the first call.
    assert impl.calls == ["get_stack_parameters"]


def test_the_loop_ending_signal_passes_through_an_sdk_s_error_handling() -> None:
    """An agent SDK catches `Exception` around a tool call and turns it into a
    tool error the model can react to, which is the right default and exactly
    the wrong behaviour for a reached limit. Deriving from BaseException is
    what makes this signal pass through untouched."""
    assert issubclass(agent_function.RunBudgetExhausted, BaseException)
    assert not issubclass(agent_function.RunBudgetExhausted, Exception)


def test_a_tool_executor_that_catches_exception_does_not_absorb_the_stop() -> None:
    """The behaviour the class hierarchy is chosen for, asserted directly.

    This drives the wrappers through a stand-in for an SDK's tool executor —
    a loop that calls tools and converts any `Exception` into a tool result,
    which is what the real one does. The signal has to survive that and reach
    the caller.

    What this does NOT establish: that strands-agents 1.61.1 propagates it the
    same way through its own async and threading plumbing. That needs the
    layer, which is not installed for these tests, so it is unverified here.
    The wall-clock budget is the backstop that does not depend on it: it is
    checked inside every tool, so a run whose stop signal were swallowed still
    ends at the deadline rather than at the Lambda timeout.
    """
    budget = budget_with(max_iterations="1")
    impl = FakeTools()
    tools = build_tools(impl, budget)
    results: List[str] = []

    def executor_loop() -> None:
        # A model that ignores every instruction and keeps calling tools.
        for _ in range(10):
            try:
                results.append(tools["search_logs"]("whatever"))
            except Exception as exc:  # noqa: BLE001 - what an SDK executor does
                results.append(f"tool error: {exc}")

    with pytest.raises(agent_function.RunBudgetExhausted):
        executor_loop()

    # One refusal reached the model, then the loop was cut off.
    assert len(results) == 2
    assert "STOP GATHERING EVIDENCE" in json.loads(results[1])["instruction"]
    assert impl.calls == ["search_logs"]


def test_the_wall_clock_is_checked_inside_the_tools_not_only_the_loop() -> None:
    """The guarantee that holds whatever the SDK does with an exception.

    Asserted against the tools module rather than restated: every tool calls
    _check_deadline before doing work, so a run ends at its deadline even if
    nothing else stops it.
    """
    source = (SOURCE_DIR / "troubleshooting_tools.py").read_text(encoding="utf-8")

    assert source.count("self._check_deadline(") == 4


def test_the_loop_ending_signal_carries_the_reason() -> None:
    budget = budget_with(max_iterations="1")
    tools = build_tools(FakeTools(), budget)
    tools["get_stack_parameters"]()
    tools["discover_log_groups"]()

    with pytest.raises(agent_function.RunBudgetExhausted) as raised:
        tools["search_logs"]("whatever")

    assert "1-tool-call limit" in raised.value.reason


def test_a_truncated_run_still_answers_in_the_three_section_format() -> None:
    """The caller gets a diagnosis saying nothing was concluded, rather than an
    error: "Not determined" with the missing evidence named is a valid answer
    and the output contract is what the UI will render."""
    answer = agent_function._TRUNCATED_ANSWER.format(
        reason="the 8-tool-call limit for one diagnosis has been reached",
        usage='{"toolCalls": 9}',
    )

    assert "## Root Cause" in answer
    assert "## Recommendations" in answer
    assert "Not determined" in answer
    assert "8-tool-call limit" in answer
    assert "<summary><strong>Evidence</strong></summary>" in answer


def test_a_tool_error_becomes_readable_text_rather_than_an_exception() -> None:
    """A raise into the agent loop gives the model an opaque failure; a
    sentence naming the log group that was not found gives it a next action."""
    budget = budget_with()
    tools = build_tools(
        FakeTools(raises=agent_function.ToolError("call discover_log_groups first")), budget
    )

    result = json.loads(tools["search_logs"]("whatever"))

    assert result["error"] == "call discover_log_groups first"
    assert result["tool"] == "search_logs"


def test_an_unexpected_tool_fault_is_labelled_as_not_being_evidence() -> None:
    """Otherwise a bug in this code becomes a finding about the deployment."""
    budget = budget_with()
    tools = build_tools(FakeTools(raises=ZeroDivisionError("oops")), budget)

    result = json.loads(tools["get_vp_record"]("vp-1"))

    assert "ZeroDivisionError" in result["error"]
    assert "not evidence about LMA" in result["note"]


def test_a_tool_fault_does_not_end_the_run() -> None:
    """One failed search should not cost the diagnosis; the agent can try
    another source."""
    budget = budget_with()
    tools = build_tools(FakeTools(raises=agent_function.ToolError("nope")), budget)

    tools["search_logs"]("whatever")

    assert budget.exhausted_reason() is None


@pytest.mark.parametrize(
    ("tool_name", "args", "expected"),
    [
        ("search_logs", {"log_group_names": "g", "lookback_hours": 0}, "lookback_hours"),
        ("search_logs", {"log_group_names": "g", "limit": 0}, "limit"),
        ("search_logs", {"log_group_names": "g", "search_terms": ""}, "search_terms"),
        ("discover_log_groups", {"components": ""}, "components"),
    ],
)
def test_an_unset_numeric_or_text_argument_is_passed_as_absent(
    tool_name: str, args: Dict[str, Any], expected: str
) -> None:
    """Strands has no optional-argument concept, so a model that omits one
    sends the signature default. Zero and the empty string must mean "use the
    configured default", not "search zero hours" or "match the empty string".
    """
    captured: Dict[str, Any] = {}

    class Capturing(FakeTools):
        def _record(self, name: str, **kwargs: Any) -> str:
            captured.update(kwargs)
            return "{}"

    build_tools(Capturing(), budget_with())[tool_name](**args)

    assert captured[expected] is None
