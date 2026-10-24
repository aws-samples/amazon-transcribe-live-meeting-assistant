#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""The LMA troubleshooting agent.

Answers "why did this fail" about a deployed LMA stack by reading its
CloudWatch logs, its virtual participant records and its CloudFormation
parameters, and reports a cause with the evidence it rests on.

ENTRY POINTS
------------
This increment deliberately ships **no** user-facing entry point. There is no
AppSync resolver, no function URL and no API Gateway route, so the only way to
invoke this function is an IAM principal holding ``lambda:InvokeFunction`` on
it, and the stack grants that to no principal. (A principal with a broad
account-level policy, such as an administrator role, has it already — this is
the absence of a grant, not a barrier.) The two entry points the design
calls for (an Admin-only stack-wide page, and a per-resource "Diagnose" button
available to the resource owner) are a separate change, because each needs an
authorization decision re-checked in this function and neither is testable
without the UI.

The event shape below is the shape those resolvers will send, so that the
authorization work is the only thing left to add:

    {
      "query":      "why did virtual participant <id> fail to join?",
      "vpId":       "<optional virtual participant id>",
      "callId":     "<optional meeting id, also the AppSync stream channel>",
      "messageId":  "<optional; streams tokens when present>"
    }

GUARDRAILS
----------
Four bounds, all enforced in this file rather than asked for in the prompt,
because a model that is asked to stop after eight tool calls sometimes makes
nine:

* ``max_iterations`` — tool calls, counted in the wrapper that every tool goes
  through. Past the limit a tool returns a stop instruction instead of doing
  work, which lets the model write its answer; a second call past the limit
  ends the loop instead of asking again, and ``RunBudgetExhausted`` explains
  why that second step is not optional.
* ``token_budget`` — cumulative input + output tokens, read from the agent's
  own accumulated usage between tool calls and checked at the same point.
* ``wall_clock_seconds`` — a deadline computed at the start of the run and
  checked both in the wrapper and inside each tool, because one Logs Insights
  query can outlast the budget by itself.
* ``max_output_tokens`` — passed to Bedrock as the per-turn output cap.

And one thing deliberately absent: **there is no whole-run retry.** Restarting
a diagnosis from the original question re-pays for every tool call it already
made, for a question whose answer did not change. A failed run reports the
failure; asking again is the operator's decision, not this function's.

Cost attribution for the Bedrock usage this agent incurs is reported in the
run's own log line rather than as a metric, because LMA emits no custom metrics
yet. When it does, ``usage`` below is the figure to publish.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

import boto3
import troubleshooting_prompt
from bedrock_compat import accepts_temperature
from botocore.config import Config as BotoCoreConfig
from troubleshooting_config import AgentLimits
from troubleshooting_discovery import classify_log_group
from troubleshooting_tools import ToolError, TroubleshootingTools

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# ── Configuration ───────────────────────────────────────────────────────────

LMA_STACK_NAME = os.environ.get("LMA_STACK_NAME", "")
VP_TABLE_NAME = os.environ.get("VIRTUAL_PARTICIPANT_TABLE_NAME", "")
PROMPT_TABLE_NAME = os.environ.get("LLM_PROMPT_TEMPLATE_TABLE_NAME", "")
APPSYNC_GRAPHQL_URL = os.environ.get("APPSYNC_GRAPHQL_URL", "")
#: This deployment's AppSync API id. Names the service-managed log
#: group /aws/appsync/apis/<apiId>, and is the same value the IAM grant
#: for that group is scoped to.
APPSYNC_API_ID = os.environ.get("APPSYNC_API_ID", "")
MODEL_ID = os.environ.get(
    "TROUBLESHOOTING_MODEL_ID", "global.anthropic.claude-haiku-4-5-20251001-v1:0"
)
BEDROCK_GUARDRAIL_ID = os.environ.get("BEDROCK_GUARDRAIL_ID", "").strip()
BEDROCK_GUARDRAIL_VERSION = os.environ.get("BEDROCK_GUARDRAIL_VERSION", "").strip()

# Diagnosis is extractive, not conversational: the same evidence should produce
# the same cause. Zero on the models that accept a temperature at all.
AGENT_TEMPERATURE = 0.0

CLIENT_CONFIG = BotoCoreConfig(retries={"mode": "adaptive", "max_attempts": 3})
# Bedrock gets the project's aggressive profile: cross-region inference
# profiles make a first attempt fail often enough that a low budget turns a
# working deployment into an intermittent one.
BEDROCK_CONFIG = BotoCoreConfig(retries={"mode": "adaptive", "max_attempts": 50})

BOTO3_SESSION = boto3.Session()
LOGS_CLIENT = BOTO3_SESSION.client("logs", config=CLIENT_CONFIG)
DYNAMODB_CLIENT = BOTO3_SESSION.client("dynamodb", config=CLIENT_CONFIG)
CLOUDFORMATION_CLIENT = BOTO3_SESSION.client("cloudformation", config=CLIENT_CONFIG)


def get_guardrail_config() -> Dict[str, str]:
    """The Bedrock guardrail id and version, or an empty dict.

    Mirrors the meeting assistant's accessor: a guardrail is only usable when
    both values are present, and an empty dict spread into the model
    configuration leaves the request exactly as it would have been.
    """
    if BEDROCK_GUARDRAIL_ID and BEDROCK_GUARDRAIL_VERSION:
        return {
            "guardrail_id": BEDROCK_GUARDRAIL_ID,
            "guardrail_version": BEDROCK_GUARDRAIL_VERSION,
        }
    return {}


# ── Budget ──────────────────────────────────────────────────────────────────


class RunBudget:
    """Tracks one run against its iteration, token and wall-clock limits.

    Deliberately not a Strands concept. The counters are incremented by the
    wrapper every tool goes through, so the limits hold whether or not the
    SDK's hook events fire with the shape this version expects — and they are
    checked at the one point where stopping is cheap and the model can still
    write an answer: immediately before a tool does any work.
    """

    def __init__(
        self,
        limits: AgentLimits,
        monotonic: Callable[[], float] = time.monotonic,
        remaining_seconds: Optional[float] = None,
    ):
        """Bound this run.

        Args:
            limits: The resolved limits.
            monotonic: Clock, injected for the tests.
            remaining_seconds: How long this Lambda invocation has left, from
                ``context.get_remaining_time_in_millis()``. When supplied it
                caps the wall clock: the configured budget is a *request*, and
                a request longer than the invocation has left would be a
                deadline the run could never reach, so the agent would be
                killed mid-answer instead of reporting that it ran out of time.
                A reserve is held back for the final model turn, which happens
                after the last tool returns.
        """
        self._limits = limits
        self._monotonic = monotonic
        self.started_at = monotonic()

        budget = float(limits.wall_clock_seconds)
        self.capped_by_invocation = False
        if remaining_seconds is not None:
            available = remaining_seconds - limits.answer_reserve_seconds
            # A floor of one second rather than zero or a negative: a deadline
            # already in the past makes the first tool refuse, which produces
            # "ran out of time" instead of an empty answer with no reason.
            available = max(1.0, available)
            if available < budget:
                budget = available
                self.capped_by_invocation = True

        self.wall_clock_seconds = budget
        self.deadline = self.started_at + budget
        self.tool_calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.stopped_because: Optional[str] = None

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def elapsed_seconds(self) -> float:
        return self._monotonic() - self.started_at

    def note_tool_call(self) -> None:
        # Not locked. Tools run one at a time in the agent's loop, and if that
        # ever changes the worst case is an undercount by one on a turn with
        # parallel calls -- which costs one extra tool call against the cap,
        # not a run that does not stop.
        self.tool_calls += 1

    def note_usage(self, input_tokens: int, output_tokens: int) -> None:
        """Record cumulative usage. Values are absolute totals, not deltas."""
        self.input_tokens = max(self.input_tokens, int(input_tokens or 0))
        self.output_tokens = max(self.output_tokens, int(output_tokens or 0))

    def exhausted_reason(self) -> Optional[str]:
        """Why this run must stop gathering evidence, or None."""
        if self.tool_calls > self._limits.max_iterations:
            return (
                f"the {self._limits.max_iterations}-tool-call limit for one "
                "diagnosis has been reached"
            )
        if self.total_tokens > self._limits.token_budget:
            return (
                f"the {self._limits.token_budget}-token budget for one diagnosis "
                "has been reached"
            )
        if self._monotonic() >= self.deadline:
            return (
                f"the {self.wall_clock_seconds:.0f}-second time budget for one "
                "diagnosis has been reached"
            )
        return None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "toolCalls": self.tool_calls,
            "inputTokens": self.input_tokens,
            "outputTokens": self.output_tokens,
            "elapsedSeconds": round(self.elapsed_seconds, 1),
            "wallClockSeconds": round(self.wall_clock_seconds, 1),
            "wallClockCappedByInvocation": self.capped_by_invocation,
            "stoppedBecause": self.stopped_because,
        }


_STOP_INSTRUCTION = (
    "STOP GATHERING EVIDENCE: {reason}. Do not call any further tools. Write "
    "your answer now from the evidence you already have, in the three-section "
    "format. If that evidence does not establish a cause, report the Root Cause "
    "as 'Not determined' and name the evidence that was still missing."
)


class RunBudgetExhausted(BaseException):
    """Unwinds the agent loop when a limit has been reached twice.

    Derived from BaseException, not Exception, deliberately. The first time a
    limit is hit the wrapper returns ``_STOP_INSTRUCTION``, which asks the
    model to write its answer — the cooperative path, and the one that produces
    a real diagnosis from the evidence already gathered. But a request to a
    model is not a limit: a model that calls another tool anyway gets another
    full turn, and a turn re-sends the whole system prompt. Asking politely
    cannot be the only thing standing between a reached limit and the Lambda
    timeout.

    So the second refusal stops the loop instead of asking again. One caveat
    on "second": refusals are counted, not turns. If the model issues several
    tool calls in parallel in the turn that crosses a limit, the first is
    refused and the second ends the run in that same turn, so it never gets
    the cooperative answer turn. That is the right trade — a limit has been
    reached either way, and the caller still gets a written answer — but it
    means a parallel-calling model sees the forced path more often than a
    serial one.

    An agent SDK catches ``Exception`` around a tool call and turns it into a
    tool error the model can react to, which is the right default and exactly
    the wrong behaviour here. So this inherits from BaseException, to pass
    through that handling untouched; ``run_diagnosis`` catches it and writes
    the answer itself.

    **What is and is not verified.** That a catch-``Exception`` tool executor
    does not absorb this is asserted directly, against a stand-in for one. That
    strands-agents propagates it unchanged through its own async and threading
    plumbing is *not*: the Strands layer is not installed for the unit tests,
    so nothing here exercises the real event loop.

    Be clear about what the fallback is, because it is easy to overstate. Past
    the deadline every tool raises instead of working, so the model's next turn
    gets an error from whatever it calls — which in practice ends a run quickly,
    but it is *not* a bound: nothing in this file stops the model taking
    further turns. If this exception were absorbed, the thing that would
    actually end the invocation is the Lambda ``Timeout``. So: this class makes
    a reached limit end promptly and cleanly, the deadline makes every further
    tool call useless, and ``Timeout`` is the final backstop. Turning that last
    one into a checked property needs an integration run against a deployed
    stack; that is tracked as issue #814 rather than claimed here.
    """

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


#: Written by run_diagnosis when the loop had to be unwound, so the caller
#: still gets the contract's three sections rather than an error page.
#:
#: It points at the function's own log for what was read, not at the
#: thinking-step trace: that trace is published only when the caller supplied
#: both a callId and a messageId, and the only way to invoke this function
#: today supplies neither. An answer that sent a reader to something that does
#: not exist would be worse than one that sent them nowhere.
_TRUNCATED_ANSWER = """## Root Cause
**Confidence:** LOW
Not determined. The investigation was stopped before it reached a conclusion: \
{reason}. Which tools ran, and what each of them returned, is in this \
function's CloudWatch log for this request; what was missing is whatever the \
next step would have read.

## Recommendations
1. Ask again with a narrower question, naming the component or the virtual \
participant id, so the available tool calls are spent on one thing.
2. If the question genuinely needs a wider search, raise the limit that was \
reached on the troubleshooting function before asking again.
3. Read this function's log for the request to see how the budget was spent \
before raising anything.

<details>
<summary><strong>Evidence</strong></summary>

**Source:** this diagnosis run
```
{usage}
```
</details>
"""


# ── AppSync streaming ───────────────────────────────────────────────────────

_ADD_CHAT_TOKEN = """
mutation AddChatToken($input: AddChatTokenInput!) {
    addChatToken(input: $input) {
        CallId
        MessageId
        Token
        IsComplete
        Sequence
        ThinkingStep {
            Type
            Content
            ToolName
            ToolInput
            ToolResult
            Success
        }
    }
}
"""


#: One AppSync client and one parsed mutation per execution environment. A
#: diagnosis publishes two steps per tool call, and building a fresh SigV4
#: client and re-parsing the document each time pays for a connection setup per
#: step for no benefit.
_APPSYNC_CLIENT: Any = None
_PARSED_MUTATION: Any = None


def _appsync_publisher() -> tuple[Any, Any]:
    """The cached AppSync client and parsed mutation, built on first use."""
    global _APPSYNC_CLIENT, _PARSED_MUTATION  # noqa: PLW0603
    if _APPSYNC_CLIENT is None:
        from asst_gql_client import AppsyncRequestsGqlClient
        from gql import gql

        _APPSYNC_CLIENT = AppsyncRequestsGqlClient(
            url=APPSYNC_GRAPHQL_URL, fetch_schema_from_transport=False
        )
        _PARSED_MUTATION = gql(_ADD_CHAT_TOKEN)
    return _APPSYNC_CLIENT, _PARSED_MUTATION


def send_thinking_step(
    call_id: str, message_id: str, step: Dict[str, Any], sequence: int
) -> None:
    """Publish one tool-call trace to the UI, reusing addChatToken.

    Best-effort by design: a diagnosis that completed but could not narrate
    itself is still a diagnosis, so every failure here is logged and swallowed.
    This is the same channel and the same piggyback-on-an-empty-token shape the
    meeting assistant uses, so the existing thinking-step panel renders these
    without any UI change.
    """
    if not (APPSYNC_GRAPHQL_URL and call_id and message_id):
        return
    try:
        client, mutation = _appsync_publisher()
        client.execute(
            mutation,
            variable_values={
                "input": {
                    "CallId": call_id,
                    "MessageId": message_id,
                    "Token": "",
                    "IsComplete": False,
                    "Sequence": sequence,
                    "ThinkingStep": {
                        "Type": step.get("type", "tool_use"),
                        "Content": step.get("content"),
                        "ToolName": step.get("tool_name"),
                        # A summary, not the arguments -- see
                        # summarise_input -- and JSON-encoded because
                        # ThinkingStepData.ToolInput is declared AWSJSON in
                        # schema.graphql, which rejects a bare string.
                        "ToolInput": json.dumps(
                            {"summary": step["tool_input_summary"]}
                        )
                        if step.get("tool_input_summary")
                        else None,
                        "ToolResult": step.get("result"),
                        "Success": step.get("success", True),
                    },
                }
            },
        )
    except Exception as exc:  # noqa: BLE001 - narration must not fail a run
        logger.warning("could not publish a thinking step: %s", exc)


class ThinkingStepHookProvider:
    """Streams tool-call traces to the UI and records token usage.

    Duck-typed against the Strands hook registry, matching the meeting
    assistant's provider: the only contract is ``register_hooks(registry)``.
    Every callback swallows its own exceptions — an observability failure must
    not end a diagnosis — and the budget counters it feeds are a *supplement*
    to the wrapper's own, never the only copy.
    """

    def __init__(
        self,
        budget: RunBudget,
        call_id: str = "",
        message_id: str = "",
        vp_id: str = "",
    ):
        self._budget = budget
        self._call_id = call_id
        self._message_id = message_id
        # The virtual participant this diagnosis was asked about, from the
        # invocation. Used to tell "the agent passed the id it was given"
        # from "the agent passed an id of its own", which decides whether the
        # value is this function's to publish.
        self._vp_id = vp_id
        # Starts high so these never collide with the token sequence numbers a
        # streaming response would use on the same channel.
        self._sequence = 100

    def register_hooks(self, registry: Any) -> None:
        from strands.hooks.events import AfterToolCallEvent, BeforeToolCallEvent

        registry.add_callback(BeforeToolCallEvent, self.on_before_tool_call)
        registry.add_callback(AfterToolCallEvent, self.on_after_tool_call)

    @staticmethod
    def _field(container: Any, name: str) -> Any:
        """Read one field whether it is a mapping key or an attribute.

        The Strands Agents SDK models a tool use and a tool result as
        ``TypedDict``s — plain dicts at runtime — while the hook *events* that
        carry them are objects. So the outer event needs attribute access and
        its payload needs key access, and a reader that assumes either one
        everywhere silently gets ``None`` from the other. Trying both is
        cheaper than depending on which it is, and survives the SDK changing
        its mind.
        """
        if container is None:
            return None
        if isinstance(container, Mapping):
            return container.get(name)
        return getattr(container, name, None)

    @classmethod
    def _tool_name(cls, event: Any) -> str:
        for container in (
            getattr(event, "selected_tool", None),
            getattr(event, "tool_use", None),
        ):
            name = cls._field(container, "tool_name") or cls._field(container, "name")
            if name:
                return str(name)
        return "unknown"

    @classmethod
    def _result_text(cls, result: Any) -> str:
        """The text a tool returned, out of a ToolResult's content blocks.

        ``ToolResult`` carries ``content`` as a list of blocks, each of which
        may hold ``text``, ``json``, an image or a document. Only the text and
        JSON blocks are of interest here, and they are joined so a tool that
        split its output across blocks still summarises correctly.
        """
        content = cls._field(result, "content")
        if content is None:
            content = cls._field(result, "output")
        if isinstance(content, str):
            return content
        if isinstance(content, (list, tuple)):
            parts: List[str] = []
            for block in content:
                text = cls._field(block, "text")
                if text is None:
                    text = cls._field(block, "json")
                if text is not None:
                    parts.append(text if isinstance(text, str) else json.dumps(text))
                elif isinstance(block, str):
                    parts.append(block)
            return "".join(parts)
        if content is None:
            return ""
        return str(content)

    @classmethod
    def _result_succeeded(cls, result: Any) -> bool:
        """Whether a tool reported success.

        **The payload is checked before the SDK's own status, and the order
        matters.** A tool that refused or faulted still *returns* — it does not
        raise — so the ``@tool`` decorator sets ``status: "success"`` on it like
        any other result. Reading the status first therefore short-circuits
        every refusal and every ToolError into a success, which is the whole
        thing this method exists to prevent. Only the payload knows.

        ``status`` and ``is_error`` are then the fallbacks, for a result this
        function did not produce — the SDK's own error path, or the shape the
        meeting assistant's hook provider reads. Unknown counts as success,
        because a narration that reported every call as failed would be worse
        than one that reported none.
        """
        try:
            payload = json.loads(cls._result_text(result) or "{}")
        except (TypeError, ValueError):
            payload = None
        if isinstance(payload, dict) and (
            "stopped" in payload or "error" in payload
        ):
            return False

        status = cls._field(result, "status")
        if isinstance(status, str):
            return status.lower() != "error"
        return not cls._field(result, "is_error")

    def on_before_tool_call(self, event: Any) -> None:
        try:
            tool_use = getattr(event, "tool_use", None)
            tool_input = self._field(tool_use, "input")
            if tool_input is None:
                tool_input = self._field(tool_use, "arguments")
            send_thinking_step(
                self._call_id,
                self._message_id,
                {
                    "type": "tool_use",
                    "tool_name": self._tool_name(event),
                    "tool_input_summary": self.summarise_input(tool_input),
                },
                self._sequence,
            )
            self._sequence += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("before-tool hook failed: %s", exc)

    #: Tool arguments whose values are safe to publish: numbers, which cannot
    #: carry log content whatever the model puts in them. Everything else is
    #: published as a length, a count, or not at all.
    _PUBLISHABLE_INPUT_FIELDS = frozenset({"lookback_hours", "limit"})

    def summarise_input(self, tool_input: Any) -> Optional[str]:
        """Describe a tool call's arguments without reproducing free text.

        The same rule as ``summarise_result``, and for the same reason: this
        stream reaches the UI without the model in the loop, so nothing the
        prompt says about what to quote applies to it. Publish a shape, a
        count, or text written by this codebase; never text the model wrote.

        Every argument here is model-written, so none is echoed.
        ``query_string``, ``search_terms`` and ``components`` appear as a
        length. Log group names appear as a count plus the components they
        classify as — a closed set, and the more useful thing for a progress
        panel. ``vp_id`` is published only when it matches the identifier the
        invocation itself supplied, which makes it this function's own value
        rather than the model's.
        """
        if not isinstance(tool_input, Mapping):
            return None
        parts: List[str] = []
        groups = tool_input.get("log_group_names")
        if groups:
            # Counted and classified, never echoed. This runs in the
            # before-tool hook, so a name here has not been checked against
            # discovery yet -- it is whatever the model wrote. The component
            # each one classifies as comes from a closed set, and is the more
            # useful thing for a progress panel anyway: "in
            # virtual-participant" rather than a path.
            names = [n.strip() for n in str(groups).split(",") if n.strip()]
            plural = "" if len(names) == 1 else "s"
            described = f"{len(names)} log group{plural}"
            components = sorted({classify_log_group(n) for n in names})
            if components:
                # One part, so the line reads "3 log groups in x, y" rather
                # than "3 log groups, in x, y".
                described += f" in {', '.join(components)}"
            parts.append(described)
        for field in ("query_string", "search_terms", "components"):
            value = tool_input.get(field)
            if value:
                # Length only. The content is the model's words, and may echo
                # a log line it has just read.
                parts.append(f"{field} ({len(str(value))} chars)")
        for field in sorted(self._PUBLISHABLE_INPUT_FIELDS):
            value = tool_input.get(field)
            if value:
                parts.append(f"{field}={value}")
        vp_id = tool_input.get("vp_id")
        if vp_id:
            # Published only when it is the identifier this invocation was
            # given. Any other value is the model's, and goes the way the
            # other model-written arguments do.
            if self._vp_id and str(vp_id) == self._vp_id:
                parts.append(f"vp_id={vp_id}")
            else:
                parts.append(f"vp_id ({len(str(vp_id))} chars)")
        return ", ".join(parts) if parts else None

    @staticmethod
    def summarise_result(content: Any) -> str:
        """Describe a tool result's shape without reproducing its content.

        The thinking-step stream is published to the UI without the model in
        the loop, so anything put here bypasses every instruction the prompt
        gives the agent about what to quote. ``search_logs`` returns raw log
        lines, and a log line can contain whatever the component that wrote it
        put there — so a prefix of the raw result is the one thing this must
        not publish.

        A shape summary is also the more useful thing to render: "read 42 rows
        from 2 log groups" is what a reader wants from a progress panel, and
        the evidence itself belongs in the diagnosis, where the agent has been
        told how to quote it.
        """
        try:
            payload = json.loads(str(content))
        except (TypeError, ValueError):
            return "completed"
        if not isinstance(payload, dict):
            return "completed"
        if "stopped" in payload:
            return f"refused: {str(payload['stopped'])[:200]}"
        if "error" in payload:
            if payload.get("fault"):
                # An unexpected exception. Its type is published and its text
                # is not: botocore messages can carry account-specific ARNs,
                # and this stream reaches the UI without the model in the loop.
                # The full text still goes to the model and to the log.
                return f"failed: {payload['fault']}"
            # A ToolError's author-written label, never its message: some of
            # those embed botocore text or a name the model supplied.
            summary = payload.get("errorSummary")
            if isinstance(summary, str) and summary:
                return f"could not gather evidence: {summary}"
            return "could not gather evidence"
        parts: List[str] = []
        for label, key in (
            ("log groups", "logGroupCount"),
            ("rows", "rowCount"),
            ("bytes scanned", "bytesScanned"),
        ):
            if isinstance(payload.get(key), int):
                parts.append(f"{payload[key]} {label}")
        if "found" in payload:
            parts.append("record found" if payload["found"] else "no record")
        if "architectureParameters" in payload:
            parts.append(
                f"{len(payload['architectureParameters'])} architecture parameters"
            )
        return ", ".join(parts) if parts else "completed"

    def on_after_tool_call(self, event: Any) -> None:
        try:
            self._record_usage(event)
            result = getattr(event, "result", None)
            send_thinking_step(
                self._call_id,
                self._message_id,
                {
                    "type": "tool_result",
                    "tool_name": self._tool_name(event),
                    "result": self.summarise_result(self._result_text(result)),
                    "success": self._result_succeeded(result),
                },
                self._sequence,
            )
            self._sequence += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("after-tool hook failed: %s", exc)

    def _record_usage(self, event: Any) -> None:
        """Feed the agent's accumulated token usage into the budget.

        Read from the SDK's own metrics rather than counted here, so the figure
        is whatever Bedrock actually billed. Attribute names are probed rather
        than assumed: a metrics-shape change in the SDK should cost the token
        budget its precision, not end the run.
        """
        usage = getattr(
            getattr(getattr(event, "agent", None), "event_loop_metrics", None),
            "accumulated_usage",
            None,
        )
        if not usage:
            return
        get = usage.get if isinstance(usage, dict) else lambda k, d=0: getattr(usage, k, d)
        self._budget.note_usage(
            get("inputTokens", 0) or get("input_tokens", 0),
            get("outputTokens", 0) or get("output_tokens", 0),
        )


# ── Tool registration ───────────────────────────────────────────────────────


def build_tools(tools: TroubleshootingTools, budget: RunBudget) -> List[Any]:
    """Wrap the four evidence tools as Strands tools, behind the budget.

    Each wrapper does three things before the tool runs: counts the call,
    refuses to do work once a limit is reached, and converts a ``ToolError``
    into readable text. That last one matters — a tool that raises into the
    agent loop gives the model an opaque failure, while a sentence saying which
    log group was not found gives it a next action.

    The ``strands`` import is local, matching the house pattern: the layer is
    absent in the unit-test environment, and the tools themselves are plain
    methods that the tests exercise directly.
    """
    from strands import tool

    def guarded(name: str, call: Callable[..., str], **kwargs: Any) -> str:
        budget.note_tool_call()
        reason = budget.exhausted_reason()
        if reason:
            already_refused = budget.stopped_because is not None
            budget.stopped_because = reason
            if already_refused:
                # The model was already told to stop and called a tool anyway.
                # Asking a second time would buy another full turn, so the loop
                # ends here and run_diagnosis writes the answer.
                logger.warning(
                    "ending the run: %s was called after the stop instruction (%s)",
                    name,
                    reason,
                )
                raise RunBudgetExhausted(reason)
            logger.info("refusing %s: %s", name, reason)
            # JSON rather than bare text so the thinking-step stream can tell
            # a refusal from a successful call: a plain string summarises as
            # "completed" and reads as a success in the UI.
            return json.dumps(
                {
                    "stopped": reason,
                    "instruction": _STOP_INSTRUCTION.format(reason=reason),
                }
            )
        try:
            return call(**kwargs)
        except ToolError as exc:
            logger.info("%s could not gather evidence: %s", name, exc)
            return json.dumps(
                {
                    "error": str(exc),
                    # The author-written label, safe for the thinking-step
                    # stream. Several of these messages embed a botocore
                    # exception's text or a log group name the model supplied.
                    "errorSummary": exc.summary,
                    "tool": name,
                }
            )
        except Exception as exc:  # noqa: BLE001 - the model gets to see this
            logger.exception("%s failed", name)
            return json.dumps(
                {
                    "error": f"{type(exc).__name__}: {exc}",
                    # The exception type alone, for the thinking-step stream.
                    # The message above can carry account-specific ARNs from
                    # botocore, and the model and the log are the right readers
                    # for that, not a UI panel.
                    "fault": type(exc).__name__,
                    "tool": name,
                    "note": "This is a fault in the tool, not evidence about LMA.",
                }
            )

    @tool
    def discover_log_groups(components: str = "") -> str:
        """List this LMA deployment's CloudWatch log groups and what each holds.

        Call this before any log search. Log group names in this solution are
        not derivable from the stack name, so the names returned here are the
        only ones that can be queried.

        Args:
            components: Optional comma-separated component filter, e.g.
                "virtual-participant,vp-launch-state-machine". Omit to list
                everything.

        Returns:
            The log groups with their owning component, how many bytes each
            holds (zero means the path is unused in this deployment, not that it
            failed), and a note on what each component's logs contain.
        """
        return guarded(
            "discover_log_groups", tools.discover_log_groups, components=components or None
        )

    @tool
    def search_logs(
        log_group_names: str,
        search_terms: str = "",
        query_string: str = "",
        lookback_hours: int = 0,
        limit: int = 0,
    ) -> str:
        """Run one CloudWatch Logs Insights query over one or more log groups.

        Use this for every "why", every timeline and every count. Supply
        search_terms to read matching lines; supply query_string for
        aggregation, field extraction or time bucketing.

        Args:
            log_group_names: Comma-separated log group names, exactly as
                discover_log_groups returned them.
            search_terms: Comma-separated literal substrings. A line matching
                any of them is returned. Ignored if query_string is given.
            query_string: A complete Logs Insights query, for example
                'fields @message | parse @message "trigger=*" as t
                | filter ispresent(t) | stats count() by t'.
            lookback_hours: How far back to search. Defaults to the
                deployment's configured window.
            limit: Maximum rows. Defaults to the configured maximum.

        Returns:
            The resolved query, the time window searched, the matching rows and
            the bytes scanned.
        """
        return guarded(
            "search_logs",
            tools.search_logs,
            log_group_names=log_group_names,
            search_terms=search_terms or None,
            query_string=query_string or None,
            lookback_hours=lookback_hours or None,
            limit=limit or None,
        )

    @tool
    def get_vp_record(vp_id: str) -> str:
        """Read one virtual participant's current record.

        Gives the current status, the prose status detail and the structured
        exit code. It does NOT give a timeline, and most of the exit taxonomy is
        not stored here — search the virtual-participant log group for that.

        Args:
            vp_id: The virtual participant id.

        Returns:
            The record, plus the field-level notes needed to read it correctly.
        """
        return guarded("get_vp_record", tools.get_vp_record, vp_id=vp_id)

    @tool
    def get_stack_parameters() -> str:
        """Read this deployment's CloudFormation parameters.

        Call this first. It says which components are deployed and therefore
        where evidence can exist at all — in particular VPLaunchType, which
        decides whether the virtual participant's logs are in the MicroVM group
        or the Fargate one.

        Returns:
            The parameters that determine the architecture, annotated, then the
            rest.
        """
        return guarded("get_stack_parameters", tools.get_stack_parameters)

    return [get_stack_parameters, discover_log_groups, get_vp_record, search_logs]


# ── Run ─────────────────────────────────────────────────────────────────────


def build_question(event: Dict[str, Any]) -> str:
    """Turn the invocation into the question the agent is asked.

    The scope identifiers are restated as facts rather than left in the prose,
    because the agent has to pass them to a tool verbatim and a model that has
    to extract an id from a sentence sometimes extracts it wrongly.
    """
    query = str(event.get("query") or "").strip()
    if not query:
        query = "Diagnose the most recent failure in this deployment."
    lines = [f"QUESTION: {query}", "", f"LMA stack: {LMA_STACK_NAME or '(not configured)'}"]
    if event.get("vpId"):
        lines.append(f"Virtual participant id in scope: {event['vpId']}")
    if event.get("callId"):
        lines.append(f"Meeting id in scope: {event['callId']}")
    lines.append(f"Current time (UTC): {datetime.now(timezone.utc).isoformat()}")
    return "\n".join(lines)


def remaining_seconds(context: Any) -> Optional[float]:
    """Seconds left in this Lambda invocation, or None if it cannot be read.

    Read from the context rather than from the function's configured Timeout,
    because the Timeout is a template value an operator can change and the
    agent's own budget has to stay inside whatever it currently is.
    """
    getter = getattr(context, "get_remaining_time_in_millis", None)
    if not callable(getter):
        return None
    try:
        return float(getter()) / 1000.0
    except Exception:  # noqa: BLE001 - an unreadable clock is not fatal
        return None


def run_diagnosis(event: Dict[str, Any], context: Any = None) -> Dict[str, Any]:
    """Diagnose one question and return the answer with its run metadata."""
    limits = AgentLimits()
    budget = RunBudget(limits, remaining_seconds=remaining_seconds(context))
    logger.info(
        "troubleshooting run starting: limits=%s budget=%s",
        json.dumps(limits.as_dict()),
        json.dumps(budget.as_dict()),
    )

    system_prompt = troubleshooting_prompt.get_system_prompt(
        DYNAMODB_CLIENT, PROMPT_TABLE_NAME
    )

    tools_impl = TroubleshootingTools(
        logs_client=LOGS_CLIENT,
        dynamodb_client=DYNAMODB_CLIENT,
        cloudformation_client=CLOUDFORMATION_CLIENT,
        stack_name=LMA_STACK_NAME,
        vp_table_name=VP_TABLE_NAME,
        limits=limits,
        appsync_api_id=APPSYNC_API_ID,
        deadline=budget.deadline,
    )

    from strands import Agent
    from strands.models import BedrockModel

    model_kwargs: Dict[str, Any] = {
        "model_id": MODEL_ID,
        "max_tokens": limits.max_output_tokens,
        # The project's aggressive Bedrock retry profile, passed explicitly:
        # BedrockModel builds its own client, so a config left on the module
        # would simply not be used and the model would run on the SDK's
        # defaults. Cross-region inference profiles make a first attempt fail
        # often enough that the difference is a working deployment versus an
        # intermittent one.
        "boto_client_config": BEDROCK_CONFIG,
        **get_guardrail_config(),
    }
    if accepts_temperature(MODEL_ID):
        model_kwargs["temperature"] = AGENT_TEMPERATURE

    hook = ThinkingStepHookProvider(
        budget,
        call_id=str(event.get("callId") or ""),
        message_id=str(event.get("messageId") or ""),
        vp_id=str(event.get("vpId") or ""),
    )
    agent = Agent(
        model=BedrockModel(**model_kwargs),
        system_prompt=system_prompt,
        tools=build_tools(tools_impl, budget),
        hooks=[hook],
    )

    try:
        try:
            answer = str(agent(build_question(event)))
        except RunBudgetExhausted as exhausted:
            # Not an error: the run reached a limit and the loop was unwound
            # rather than paying for another turn. The caller still gets the
            # output contract's three sections, saying plainly that nothing was
            # concluded.
            answer = _TRUNCATED_ANSWER.format(
                reason=exhausted.reason, usage=json.dumps(budget.as_dict(), indent=2)
            )
    finally:
        # In a `finally` so a run that raised still records what it spent. A
        # failed diagnosis has usually made several Bedrock calls, and that is
        # exactly the cost somebody wants to see.
        #
        # Formatted into the message rather than passed as `extra`: this
        # function logs through the stdlib logger to a log group with no JSON
        # format, where `extra` fields are dropped. These numbers are the only
        # record of what a diagnosis cost, so they have to be in the line.
        logger.info(
            "troubleshooting run finished: model=%s budget=%s",
            MODEL_ID,
            json.dumps(budget.as_dict()),
        )

    return {
        "answer": answer,
        "modelId": MODEL_ID,
        "usage": budget.as_dict(),
        "truncated": budget.stopped_because is not None,
    }


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """Lambda entry point.

    Returns 200 with the diagnosis, 400 when the deployment is not configured
    for diagnosis, or 500 when the run itself failed. A failure is not retried:
    see the module docstring.

    **An error body carries a fixed message and an exception type, never the
    exception's text.** The same rule the thinking-step stream follows, and for
    a reason that is about to matter more: today this body only reaches an IAM
    principal holding ``lambda:InvokeFunction``, but the resolvers the design
    calls for will forward it to a browser, and a botocore message can carry an
    account-specific ARN. The full text goes to the log, which is where someone
    diagnosing the diagnoser should be looking anyway.
    """
    try:
        result = run_diagnosis(event or {}, context)
    except troubleshooting_prompt.PromptUnavailableError as exc:
        logger.error("refusing to diagnose without a system prompt: %s", exc)
        return {
            "statusCode": 400,
            "body": json.dumps(
                {
                    "error": "The troubleshooting agent's system prompt is not "
                    "available, so it will not run. The function's log says "
                    "which item could not be read.",
                    "errorType": type(exc).__name__,
                }
            ),
        }
    except Exception as exc:  # noqa: BLE001 - one failed run, reported as one
        logger.exception("the troubleshooting run failed")
        return {
            "statusCode": 500,
            "body": json.dumps(
                {
                    "error": "The diagnosis could not be completed. The "
                    "function's log carries the failure.",
                    "errorType": type(exc).__name__,
                }
            ),
        }
    return {"statusCode": 200, "body": json.dumps(result)}
