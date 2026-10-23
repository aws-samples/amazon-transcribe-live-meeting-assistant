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
it — which, in a default deployment, is nobody. The two entry points the design
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
  work, which ends the run with an answer rather than an exception.
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
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

import boto3
import troubleshooting_prompt
from bedrock_compat import accepts_temperature
from botocore.config import Config as BotoCoreConfig
from troubleshooting_config import AgentLimits
from troubleshooting_tools import ToolError, TroubleshootingTools

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# ── Configuration ───────────────────────────────────────────────────────────

LMA_STACK_NAME = os.environ.get("LMA_STACK_NAME", "")
VP_TABLE_NAME = os.environ.get("VIRTUAL_PARTICIPANT_TABLE_NAME", "")
PROMPT_TABLE_NAME = os.environ.get("LLM_PROMPT_TEMPLATE_TABLE_NAME", "")
APPSYNC_GRAPHQL_URL = os.environ.get("APPSYNC_GRAPHQL_URL", "")
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

    def __init__(self, limits: AgentLimits, monotonic: Callable[[], float] = time.monotonic):
        self._limits = limits
        self._monotonic = monotonic
        self.started_at = monotonic()
        self.deadline = self.started_at + limits.wall_clock_seconds
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
                f"the {self._limits.wall_clock_seconds}-second time budget for one "
                "diagnosis has been reached"
            )
        return None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "toolCalls": self.tool_calls,
            "inputTokens": self.input_tokens,
            "outputTokens": self.output_tokens,
            "elapsedSeconds": round(self.elapsed_seconds, 1),
            "stoppedBecause": self.stopped_because,
        }


_STOP_INSTRUCTION = (
    "STOP GATHERING EVIDENCE: {reason}. Do not call any further tools. Write "
    "your answer now from the evidence you already have, in the three-section "
    "format. If that evidence does not establish a cause, report the Root Cause "
    "as 'Not determined' and name the evidence that was still missing."
)


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
                        "ToolInput": json.dumps(step["tool_input"])
                        if step.get("tool_input")
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

    def __init__(self, budget: RunBudget, call_id: str = "", message_id: str = ""):
        self._budget = budget
        self._call_id = call_id
        self._message_id = message_id
        # Starts high so these never collide with the token sequence numbers a
        # streaming response would use on the same channel.
        self._sequence = 100

    def register_hooks(self, registry: Any) -> None:
        from strands.hooks.events import AfterToolCallEvent, BeforeToolCallEvent

        registry.add_callback(BeforeToolCallEvent, self.on_before_tool_call)
        registry.add_callback(AfterToolCallEvent, self.on_after_tool_call)

    @staticmethod
    def _tool_name(event: Any) -> str:
        selected = getattr(event, "selected_tool", None)
        if not selected:
            return "unknown"
        return (
            getattr(selected, "tool_name", None)
            or getattr(selected, "name", None)
            or "unknown"
        )

    def on_before_tool_call(self, event: Any) -> None:
        try:
            tool_use = getattr(event, "tool_use", None)
            tool_input = getattr(tool_use, "input", None) or getattr(
                tool_use, "arguments", None
            )
            send_thinking_step(
                self._call_id,
                self._message_id,
                {
                    "type": "tool_use",
                    "tool_name": self._tool_name(event),
                    "tool_input": tool_input or {},
                },
                self._sequence,
            )
            self._sequence += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("before-tool hook failed: %s", exc)

    def on_after_tool_call(self, event: Any) -> None:
        try:
            self._record_usage(event)
            result = getattr(event, "result", None)
            content = (
                getattr(result, "content", None)
                or getattr(result, "output", None)
                or result
            )
            send_thinking_step(
                self._call_id,
                self._message_id,
                {
                    "type": "tool_result",
                    "tool_name": self._tool_name(event),
                    "result": str(content)[:500],
                    "success": not getattr(result, "is_error", False),
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
            budget.stopped_because = reason
            logger.info("refusing %s: %s", name, reason)
            return _STOP_INSTRUCTION.format(reason=reason)
        try:
            return call(**kwargs)
        except ToolError as exc:
            logger.info("%s could not gather evidence: %s", name, exc)
            return json.dumps({"error": str(exc), "tool": name})
        except Exception as exc:  # noqa: BLE001 - the model gets to see this
            logger.exception("%s failed", name)
            return json.dumps(
                {
                    "error": f"{type(exc).__name__}: {exc}",
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


def run_diagnosis(event: Dict[str, Any]) -> Dict[str, Any]:
    """Diagnose one question and return the answer with its run metadata."""
    limits = AgentLimits()
    budget = RunBudget(limits)
    logger.info("troubleshooting run starting", extra={"limits": limits.as_dict()})

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
        graphql_url=APPSYNC_GRAPHQL_URL,
        deadline=budget.deadline,
    )

    from strands import Agent
    from strands.models import BedrockModel

    model_kwargs: Dict[str, Any] = {
        "model_id": MODEL_ID,
        "max_tokens": limits.max_output_tokens,
        **get_guardrail_config(),
    }
    if accepts_temperature(MODEL_ID):
        model_kwargs["temperature"] = AGENT_TEMPERATURE

    hook = ThinkingStepHookProvider(
        budget,
        call_id=str(event.get("callId") or ""),
        message_id=str(event.get("messageId") or ""),
    )
    agent = Agent(
        model=BedrockModel(**model_kwargs),
        system_prompt=system_prompt,
        tools=build_tools(tools_impl, budget),
        hooks=[hook],
    )

    answer = str(agent(build_question(event)))

    logger.info(
        "troubleshooting run finished",
        extra={"budget": budget.as_dict(), "modelId": MODEL_ID},
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
    """
    try:
        result = run_diagnosis(event or {})
    except troubleshooting_prompt.PromptUnavailableError as exc:
        logger.error("refusing to diagnose without a system prompt: %s", exc)
        return {
            "statusCode": 400,
            "body": json.dumps(
                {
                    "error": "The troubleshooting agent's system prompt is not "
                    f"available: {exc}",
                }
            ),
        }
    except Exception as exc:  # noqa: BLE001 - one failed run, reported as one
        logger.exception("the troubleshooting run failed")
        return {
            "statusCode": 500,
            "body": json.dumps({"error": f"{type(exc).__name__}: {exc}"}),
        }
    return {"statusCode": 200, "body": json.dumps(result)}
