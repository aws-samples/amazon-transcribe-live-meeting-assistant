#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""The troubleshooting agent's run budget, through the real Strands event loop.

Everything else in ``test_troubleshooting_agent_function.py`` drives the tool
wrappers through a stub of the ``strands`` package, which is fast and checks
the wrappers' own logic but says nothing about how the SDK schedules them. This
suite runs ``run_diagnosis`` end to end with the real ``strands.Agent`` — its
event loop, its concurrent tool executor and its ``@tool`` decorator — and
replaces only the Bedrock model, with a scripted one.

The scripted model checks one thing Bedrock checks: that every ``toolUse`` in
the history it is sent is answered by a ``toolResult`` in the message after
it. Bedrock rejects a request that breaks that rule, so a run that leaves a
call unanswered fails here the same way it fails against the service.

Requires strands-agents, at the version the layer pins
(``lma-meetingassist-setup-stack/strands_layer/requirements.txt``); ``make
setup-cli-dev`` installs it. Without it the suite is skipped, and ``make
test-lambdas`` runs pytest with ``-rs`` so the skip is listed.
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any, AsyncIterator, Dict, List, Optional
from unittest import mock

import pytest

strands = pytest.importorskip(
    "strands",
    reason="strands-agents is not installed, so the real-SDK budget tests cannot run "
    "(make setup-cli-dev installs the layer's pinned version)",
)
from strands.models import Model  # noqa: E402

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("LMA_STACK_NAME", "LMA-Test")

with mock.patch("boto3.Session", mock.MagicMock()):
    import troubleshooting_agent_function as agent_function  # noqa: E402

from troubleshooting_config import AgentLimits  # noqa: E402


class UnansweredToolUse(Exception):
    """What Bedrock's ValidationException reports, raised by the fake model."""


def _check_every_tool_use_is_answered(messages: List[Dict[str, Any]]) -> None:
    for index, message in enumerate(messages):
        if message.get("role") != "assistant":
            continue
        asked = {
            block["toolUse"]["toolUseId"]
            for block in message.get("content", [])
            if "toolUse" in block
        }
        if not asked:
            continue
        following = messages[index + 1] if index + 1 < len(messages) else {"content": []}
        answered = {
            block["toolResult"]["toolUseId"]
            for block in following.get("content", [])
            if "toolResult" in block
        }
        missing = asked - answered
        if missing:
            raise UnansweredToolUse(
                f"Expected toolResult blocks at messages.{index + 1}.content for the "
                f"following Ids: {', '.join(sorted(missing))}"
            )


class ScriptedModel(Model):
    """A model that plays back one response per turn.

    Each turn is either a string (a final text answer) or a list of
    ``(tool_name, input)`` pairs, which are emitted as parallel ``toolUse``
    blocks in a single response — the shape Haiku produced on a live stack.
    """

    def __init__(
        self,
        turns: List[Any],
        input_tokens_per_turn: int = 10,
        on_turn: Optional[Any] = None,
    ):
        self._turns = list(turns)
        self._input_tokens = input_tokens_per_turn
        self._on_turn = on_turn
        self.calls = 0
        self.config: Dict[str, Any] = {"model_id": "scripted"}

    def update_config(self, **model_config: Any) -> None:
        self.config.update(model_config)

    def get_config(self) -> Any:
        return self.config

    def structured_output(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    async def stream(self, messages: Any, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        _check_every_tool_use_is_answered(messages)
        if not self._turns:
            raise AssertionError("the model was called after its script ran out")
        self.calls += 1
        if self._on_turn:
            self._on_turn(self.calls)
        turn = self._turns.pop(0)
        yield {"messageStart": {"role": "assistant"}}
        if isinstance(turn, str):
            yield {"contentBlockStart": {"start": {}}}
            yield {"contentBlockDelta": {"delta": {"text": turn}}}
            yield {"contentBlockStop": {}}
            stop_reason = "end_turn"
        else:
            for name, tool_input in turn:
                tool_use = {"toolUseId": f"tooluse_{uuid.uuid4().hex}", "name": name}
                yield {"contentBlockStart": {"start": {"toolUse": tool_use}}}
                delta = {"toolUse": {"input": json.dumps(tool_input)}}
                yield {"contentBlockDelta": {"delta": delta}}
                yield {"contentBlockStop": {}}
            stop_reason = "tool_use"
        yield {"messageStop": {"stopReason": stop_reason}}
        yield {
            "metadata": {
                "usage": {
                    "inputTokens": self._input_tokens,
                    "outputTokens": 1,
                    "totalTokens": self._input_tokens + 1,
                },
                "metrics": {"latencyMs": 1},
            }
        }


class FakeTools:
    """Stands in for TroubleshootingTools; records the calls that did work."""

    def __init__(self) -> None:
        self.calls: List[str] = []

    def _record(self, name: str) -> str:
        self.calls.append(name)
        return json.dumps({"ok": name})

    def discover_log_groups(self, **_: Any) -> str:
        return self._record("discover_log_groups")

    def search_logs(self, **_: Any) -> str:
        return self._record("search_logs")

    def get_vp_record(self, **_: Any) -> str:
        return self._record("get_vp_record")

    def get_stack_parameters(self, **_: Any) -> str:
        return self._record("get_stack_parameters")


SEARCH = ("search_logs", {"log_group_names": "/aws/lambda/x", "search_terms": "ERROR"})
PARAMS = ("get_stack_parameters", {})
DISCOVER = ("discover_log_groups", {})
ANSWER = "## Root Cause\n**Confidence:** LOW\nNot determined from the evidence gathered."


def run(
    model: ScriptedModel,
    impl: FakeTools,
    clock: Optional[Any] = None,
    **env: str,
) -> Dict[str, Any]:
    """Run the real run_diagnosis with the real Strands Agent and a fake model.

    Returns the result and the conversation the agent finished with.
    """
    environ = {f"TROUBLESHOOTING_{k.upper()}": v for k, v in env.items()}
    agents: List[Any] = []
    real_agent = strands.Agent

    def capture(*args: Any, **kwargs: Any) -> Any:
        agent = real_agent(*args, **kwargs)
        agents.append(agent)
        return agent

    real_budget = agent_function.RunBudget

    def budget(limits: AgentLimits, remaining_seconds: Optional[float] = None) -> Any:
        if clock is None:
            return real_budget(limits, remaining_seconds=remaining_seconds)
        return real_budget(limits, monotonic=clock, remaining_seconds=remaining_seconds)

    with (
        mock.patch.dict("os.environ", environ, clear=False),
        mock.patch("strands.models.BedrockModel", return_value=model),
        mock.patch("strands.Agent", side_effect=capture),
        mock.patch.object(agent_function, "TroubleshootingTools", return_value=impl),
        mock.patch.object(agent_function, "RunBudget", side_effect=budget),
        mock.patch.object(
            agent_function.troubleshooting_prompt, "get_system_prompt", return_value="prompt"
        ),
    ):
        result = agent_function.run_diagnosis({"query": "why?", "vpId": "vp-1"})
    messages = agents[0].messages if agents else []
    _check_every_tool_use_is_answered(messages)
    return {"result": result, "messages": messages}


def tool_results(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Every tool result in the conversation, decoded."""
    out = []
    for message in messages:
        for block in message.get("content", []):
            if "toolResult" in block:
                text = "".join(c.get("text", "") for c in block["toolResult"]["content"])
                out.append(json.loads(text))
    return out


# ── the turn that crosses the limit ─────────────────────────────────────────


def test_parallel_calls_in_the_crossing_turn_all_get_the_stop_instruction() -> None:
    """The shape that failed on a live stack: two parallel calls in the turn
    that crosses the limit. Neither has ignored anything — they were issued
    together, before either result existed — so both are told to stop, the
    model gets its answer turn, and that answer is what the caller receives."""
    model = ScriptedModel([[PARAMS], [DISCOVER], [SEARCH, SEARCH], ANSWER])
    impl = FakeTools()

    outcome = run(model, impl, max_iterations="2")

    results = tool_results(outcome["messages"])
    assert len(results) == 4
    assert [r.get("ok") for r in results[:2]] == ["get_stack_parameters", "discover_log_groups"]
    for refused in results[2:]:
        assert "STOP GATHERING EVIDENCE" in refused["instruction"]
    assert impl.calls == ["get_stack_parameters", "discover_log_groups"]
    assert model.calls == 4
    assert outcome["result"]["answer"].strip() == ANSWER
    assert outcome["result"]["truncated"] is True
    assert outcome["result"]["usage"]["endedBecause"] is None


def test_parallel_calls_in_a_later_turn_end_the_run_with_the_truncated_answer() -> None:
    """The model ignores the stop instruction and issues two more parallel
    calls. Both still get a result — so the conversation stays valid — the loop
    ends without another model call, and the caller gets the three-section
    answer saying nothing was concluded rather than a failure."""
    model = ScriptedModel([[PARAMS], [SEARCH, SEARCH], [SEARCH, SEARCH]])
    impl = FakeTools()

    outcome = run(model, impl, max_iterations="1")

    results = tool_results(outcome["messages"])
    assert len(results) == 5
    for refused in results[1:3]:
        assert "STOP GATHERING EVIDENCE" in refused["instruction"]
    for ended in results[3:]:
        assert "This diagnosis has ended" in ended["instruction"]
    # The script holds no fourth turn, so a further model call would fail.
    assert model.calls == 3
    assert impl.calls == ["get_stack_parameters"]
    answer = outcome["result"]["answer"]
    assert answer.startswith("## Root Cause")
    assert "Not determined" in answer
    assert "## Recommendations" in answer
    assert "1-tool-call limit" in answer
    assert outcome["result"]["truncated"] is True
    assert "1-tool-call limit" in outcome["result"]["usage"]["endedBecause"]


def test_the_live_sequence_at_the_default_limit_returns_an_answer() -> None:
    """Eight calls, then calls nine and ten in one turn, at the default limit."""
    turns: List[Any] = [[PARAMS], [("get_vp_record", {"vp_id": "vp-1"})], [DISCOVER]]
    turns += [[SEARCH]] * 5 + [[SEARCH, SEARCH], ANSWER]
    model = ScriptedModel(turns)
    impl = FakeTools()

    outcome = run(model, impl)

    assert len(impl.calls) == 8
    assert outcome["result"]["answer"].strip() == ANSWER
    assert outcome["result"]["usage"]["toolCalls"] == 10


# ── the other two budgets take the same path ────────────────────────────────


def test_a_token_budget_overrun_ends_the_run_the_same_way() -> None:
    """Usage is read from the SDK's accumulated metrics in the after-tool hook,
    so this also checks that hook's reading against the real metrics object."""
    model = ScriptedModel([[PARAMS], [SEARCH], [SEARCH, SEARCH]], input_tokens_per_turn=500)
    impl = FakeTools()

    outcome = run(model, impl, token_budget="100")

    assert impl.calls == ["get_stack_parameters"]
    assert model.calls == 3
    assert "100-token budget" in outcome["result"]["usage"]["endedBecause"]
    assert "Not determined" in outcome["result"]["answer"]


def test_a_wall_clock_overrun_ends_the_run_the_same_way() -> None:
    """The clock advances a minute per model call, past a 90-second budget."""

    class Clock:
        now = 0.0

        def __call__(self) -> float:
            return self.now

    clock = Clock()

    def advance(_: int) -> None:
        clock.now += 60.0

    model = ScriptedModel([[PARAMS], [SEARCH], [SEARCH], [SEARCH]], on_turn=advance)
    impl = FakeTools()

    outcome = run(model, impl, clock=clock, wall_clock_seconds="90")

    # Turn 1 runs at 60s; turn 2 at 120s is refused; turn 3 ends the run.
    assert impl.calls == ["get_stack_parameters"]
    assert model.calls == 3
    assert "90-second time budget" in outcome["result"]["usage"]["endedBecause"]
    assert "Not determined" in outcome["result"]["answer"]


def test_a_run_within_its_limits_returns_the_model_s_answer() -> None:
    model = ScriptedModel([[PARAMS], [DISCOVER, SEARCH], ANSWER])
    impl = FakeTools()

    outcome = run(model, impl)

    assert sorted(impl.calls) == ["discover_log_groups", "get_stack_parameters", "search_logs"]
    assert outcome["result"]["answer"].strip() == ANSWER
    assert outcome["result"]["truncated"] is False


def test_the_tool_context_is_not_offered_to_the_model_as_an_argument() -> None:
    """``tool_context`` is injected by Strands; if it appeared in the tool spec
    the model would be invited to supply it."""
    with mock.patch("boto3.Session", mock.MagicMock()):
        tools = agent_function.build_tools(FakeTools(), agent_function.RunBudget(AgentLimits()))
    for fn in tools:
        schema = fn.tool_spec["inputSchema"]["json"]
        assert "tool_context" not in schema.get("properties", {}), fn.tool_name
