#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""The troubleshooting agent's evidence-gathering tools.

Four tools, all read-only, all bounded. They are written as methods on a plain
class that takes its AWS clients by constructor argument, so the whole surface
is testable without the Strands layer, without Bedrock and without AWS: the
agent wiring in ``troubleshooting_agent_function`` is what turns them into
``@tool`` callables.

WHY LOGS INSIGHTS AND NOT ``FilterLogEvents``
---------------------------------------------
``FilterLogEvents`` can only match a pattern and hand back matching lines, one
log group at a time. Three of LMA's most common questions are not that shape:

* "which triggers are the Teams joins failing with today, and how many of
  each" — a ``parse`` plus ``stats count() by`` over the VP container group.
* "did the transcript pipeline and the transcriber see the same meeting" — one
  query over two log groups, with the results interleaved by timestamp.
* "when did this start" — ``stats count() by bin(5m)``.

Insights does all three in one call. The cost is that it is asynchronous, so
the poll loop below is the price of admission, and the reason
``query_timeout_seconds`` exists.

ON CAPS
-------
Each tool clamps its own result twice: once by asking the API for less (the
``limit`` on the Insights query, the ``max_log_groups`` cut in discovery), and
once by truncating what it returns (``max_tool_result_chars``). The first
bounds spend, the second bounds the model's context. They are not the same
limit and conflating them is how a cheap query still blows the context window.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence

import troubleshooting_discovery as discovery
from troubleshooting_config import AgentLimits, truncate

# Logs Insights accepts at most 50 log groups in one query.
_INSIGHTS_MAX_GROUPS = 50

# Terminal states of a Logs Insights query.
_QUERY_DONE = frozenset({"Complete", "Failed", "Cancelled", "Timeout", "Unknown"})

# Fields a default (non-expert) query selects. @logStream is included because
# the VP and transcriber groups carry one stream per meeting, so the stream
# name is itself the correlation key.
_DEFAULT_FIELDS = "@timestamp, @logStream, @message"


class ToolError(RuntimeError):
    """A tool could not gather its evidence.

    Raised rather than returned so the caller decides whether the agent sees
    a readable failure or the run ends. Tools never fabricate an empty result
    to stand in for an error: "no matching log lines" and "the query failed"
    lead to opposite conclusions, and a model cannot tell them apart if both
    arrive as an empty list.
    """


def _escape_insights_string(text: str) -> str:
    """Escape a literal for use inside a Logs Insights double-quoted string."""
    return text.replace("\\", "\\\\").replace('"', '\\"')


def build_search_query(
    search_terms: Sequence[str],
    limit: int,
    *,
    fields: str = _DEFAULT_FIELDS,
) -> str:
    """Build a Logs Insights query that returns lines matching any term.

    Terms are matched as literal substrings via ``like "…"`` rather than as
    regular expressions, because the terms the agent will reach for are
    bracket tags and log prefixes — ``[TRANSCRIBING]``, ``VP exit:`` — whose
    punctuation would otherwise be interpreted as regex syntax and silently
    match the wrong thing.
    """
    select = f"fields {fields}"
    if not search_terms:
        return f"{select}\n| sort @timestamp desc\n| limit {limit}"
    clauses = " or ".join(
        f'@message like "{_escape_insights_string(term)}"' for term in search_terms
    )
    return f"{select}\n| filter {clauses}\n| sort @timestamp desc\n| limit {limit}"


def _as_term_list(search_terms: Any) -> List[str]:
    """Normalise the agent's `search_terms` argument into a list of strings.

    The model supplies this, so it arrives as a list, a comma-separated
    string, or a single string depending on how the turn went. All three are
    accepted; nothing else is.
    """
    if search_terms is None:
        return []
    if isinstance(search_terms, str):
        parts = [p.strip() for p in search_terms.split(",")]
        return [p for p in parts if p]
    if isinstance(search_terms, Iterable):
        return [str(p).strip() for p in search_terms if str(p).strip()]
    return []


class TroubleshootingTools:
    """Read-only evidence gathering against one LMA deployment.

    Args:
        logs_client: CloudWatch Logs client.
        dynamodb_client: DynamoDB client (low-level, wire format).
        cloudformation_client: CloudFormation client.
        stack_name: The LMA parent stack name.
        vp_table_name: The VirtualParticipant DynamoDB table name.
        limits: Resolved caps for this run.
        graphql_url: AppSync endpoint, used to find the AppSync log group.
        deadline: ``time.monotonic()`` value past which tools refuse to start
            new work. The agent's wall-clock budget, enforced here as well as
            in the agent loop, because one slow Insights query can outlast the
            budget on its own.
    """

    def __init__(
        self,
        *,
        logs_client: Any,
        dynamodb_client: Any,
        cloudformation_client: Any,
        stack_name: str,
        vp_table_name: str,
        limits: AgentLimits,
        graphql_url: str = "",
        deadline: Optional[float] = None,
        sleep: Any = time.sleep,
        monotonic: Any = time.monotonic,
    ) -> None:
        self._logs = logs_client
        self._ddb = dynamodb_client
        self._cfn = cloudformation_client
        self._stack_name = stack_name
        self._vp_table_name = vp_table_name
        self._limits = limits
        self._graphql_url = graphql_url
        self._deadline = deadline
        self._sleep = sleep
        self._monotonic = monotonic
        # Cached across tool calls within one run: discovery is the same answer
        # every time and the model reliably asks for it more than once.
        self._discovered: Optional[List[discovery.LogGroupInfo]] = None

    # ── Shared helpers ─────────────────────────────────────────────────────

    def _check_deadline(self, what: str) -> None:
        if self._deadline is not None and self._monotonic() >= self._deadline:
            raise ToolError(
                f"the time budget for this diagnosis was exhausted before {what} "
                "could run; report what has been established so far and name "
                "this as the evidence that is missing"
            )

    def _render(self, payload: Dict[str, Any]) -> str:
        """Serialise a tool result, capped at the configured size."""
        return truncate(
            json.dumps(payload, default=str, indent=None),
            self._limits.max_tool_result_chars,
        )

    def _known_log_group_names(self) -> set[str]:
        return {g.name for g in self._all_log_groups()}

    def _all_log_groups(self) -> List[discovery.LogGroupInfo]:
        """Every log group in this deployment, discovered at most once per run.

        Cached because the answer cannot change inside one diagnosis and the
        model reliably asks for it more than once — and because every
        ``search_logs`` call validates its arguments against this list, so an
        uncached lookup would re-page ``describe_log_groups`` on every query.
        """
        if self._discovered is None:
            self._discovered = discovery.discover_log_groups(
                self._logs,
                self._stack_name,
                max_log_groups=self._limits.max_log_groups,
                graphql_url=self._graphql_url,
            )
        return self._discovered

    # ── Tool 1: discover_log_groups ────────────────────────────────────────

    def discover_log_groups(self, components: Any = None) -> str:
        """List this deployment's CloudWatch log groups with their owners.

        Args:
            components: Optional component names to filter by.

        Returns:
            A JSON object with the groups, the component notes that say what
            each one holds, and a count of how many hold no data at all.
        """
        self._check_deadline("log group discovery")
        # Filtering is applied to the cached full list rather than pushed into
        # a second discovery pass: a filtered pass must not be what populates
        # the cache, or a later search_logs would validate its log group names
        # against only the components the model happened to ask about first.
        wanted = _as_term_list(components) or None
        groups = self._all_log_groups()
        if wanted is not None:
            allowed = set(wanted)
            groups = [g for g in groups if g.component in allowed]

        present = [g for g in groups if g.has_data]
        empty = [g for g in groups if not g.has_data]
        return self._render(
            {
                "stackName": self._stack_name,
                "logGroupCount": len(groups),
                "logGroups": [g.to_dict() for g in groups],
                "componentNotes": {
                    c: discovery.COMPONENT_NOTES[c]
                    for c in sorted({g.component for g in groups})
                    if c in discovery.COMPONENT_NOTES
                },
                "emptyLogGroupCount": len(empty),
                "guidance": (
                    f"{len(present)} of {len(groups)} groups hold data. A group "
                    "with hasData=false has never been written to, which means "
                    "this deployment does not use that path — it is not "
                    "evidence that the component failed. Query only the groups "
                    "that hold data."
                ),
            }
        )

    # ── Tool 2: search_logs ────────────────────────────────────────────────

    def search_logs(
        self,
        log_group_names: Any,
        search_terms: Any = None,
        query_string: Any = None,
        lookback_hours: Any = None,
        limit: Any = None,
    ) -> str:
        """Run one CloudWatch Logs Insights query across one or more groups.

        Args:
            log_group_names: Groups to query. Must be names returned by
                `discover_log_groups`.
            search_terms: Literal substrings; a line matching any of them is
                returned. Ignored when `query_string` is supplied.
            query_string: A complete Logs Insights query, for aggregation
                (`stats count() by`), field extraction (`parse`) or bucketing
                (`bin()`). Use this when counting rather than reading.
            lookback_hours: How far back to search.
            limit: Maximum rows to return.

        Returns:
            A JSON object with the resolved query, the time window, the rows,
            and the bytes CloudWatch scanned to produce them.
        """
        self._check_deadline("a log search")

        requested = _as_term_list(log_group_names)
        if not requested:
            raise ToolError(
                "no log group was named. Call discover_log_groups first and "
                "query the groups it returns; log group names in this solution "
                "cannot be guessed from the stack name."
            )

        known = self._known_log_group_names()
        groups = [name for name in requested if name in known]
        unknown = [name for name in requested if name not in known]
        if not groups:
            raise ToolError(
                "none of the named log groups exist in this deployment: "
                f"{unknown}. Call discover_log_groups and use the names it "
                "returns verbatim."
            )
        groups = groups[: min(_INSIGHTS_MAX_GROUPS, self._limits.max_log_groups)]

        hours = self._limits.resolve_lookback_hours(lookback_hours)
        row_limit = self._resolve_limit(limit)

        if isinstance(query_string, str) and query_string.strip():
            resolved_query = query_string.strip()
        else:
            resolved_query = build_search_query(_as_term_list(search_terms), row_limit)

        end_time = int(time.time())
        start_time = end_time - hours * 3600

        rows, status, scanned = self._run_insights_query(
            log_group_names=groups,
            query_string=resolved_query,
            start_time=start_time,
            end_time=end_time,
            row_limit=row_limit,
        )

        return self._render(
            {
                "logGroupNames": groups,
                "unknownLogGroupNames": unknown,
                "queryString": resolved_query,
                "lookbackHours": hours,
                "status": status,
                "rowCount": len(rows),
                "bytesScanned": scanned,
                "rows": rows,
                "guidance": (
                    "An empty rowCount means no line in this window matched. "
                    "That is not a cause; it narrows where the cause can be. "
                    "Widen lookbackHours, drop a term, or query a different "
                    "component before concluding anything from it."
                )
                if not rows
                else None,
            }
        )

    def _resolve_limit(self, limit: Any) -> int:
        maximum = self._limits.max_log_events
        if limit is None:
            return maximum
        try:
            value = int(float(limit))
        except (TypeError, ValueError):
            return maximum
        if value < 1:
            return maximum
        return min(value, maximum)

    def _run_insights_query(
        self,
        *,
        log_group_names: Sequence[str],
        query_string: str,
        start_time: int,
        end_time: int,
        row_limit: int,
    ) -> tuple[List[Dict[str, str]], str, int]:
        """Start a Logs Insights query and poll it to completion.

        The query is cancelled on our own timeout rather than left running:
        an account has a bounded number of concurrent Insights queries, and
        abandoning them is how one runaway diagnosis starves the next.
        """
        started = self._logs.start_query(
            logGroupNames=list(log_group_names),
            startTime=start_time,
            endTime=end_time,
            queryString=query_string,
            limit=row_limit,
        )
        query_id = started.get("queryId")
        if not query_id:
            raise ToolError("CloudWatch Logs did not return a query id")

        waited = 0.0
        while True:
            response = self._logs.get_query_results(queryId=query_id)
            status = response.get("status", "Unknown")
            if status in _QUERY_DONE:
                break
            if waited >= self._limits.query_timeout_seconds:
                self._stop_query(query_id)
                raise ToolError(
                    f"the query was still {status} after "
                    f"{self._limits.query_timeout_seconds}s and was cancelled. "
                    "Narrow it: fewer log groups, a shorter lookbackHours, or a "
                    "more specific search term."
                )
            self._sleep(self._limits.query_poll_seconds)
            waited += self._limits.query_poll_seconds

        if status in ("Failed", "Cancelled"):
            raise ToolError(f"the Logs Insights query ended as {status}")

        statistics = response.get("statistics") or {}
        rows = [
            self._row_to_dict(row) for row in (response.get("results") or [])[:row_limit]
        ]
        return rows, status, int(statistics.get("bytesScanned") or 0)

    def _stop_query(self, query_id: str) -> None:
        """Cancel a query, best-effort.

        The caller raises a ToolError either way, so a failure to cancel is
        swallowed: it must not replace the timeout message, which is the one
        the agent needs in order to narrow its next query.
        """
        try:
            self._logs.stop_query(queryId=query_id)
        except Exception:  # nosec B110 - noqa: BLE001
            # The outcome is already decided by the caller; the only thing
            # cancelling adds is returning Insights concurrency sooner.
            pass

    def _row_to_dict(self, row: Sequence[Dict[str, str]]) -> Dict[str, str]:
        """Flatten one Insights result row, truncating long messages.

        ``@ptr`` is dropped: it is an opaque pointer used to fetch the
        surrounding log record, which this tool cannot do, so it is pure
        context cost.
        """
        out: Dict[str, str] = {}
        for field in row:
            name = field.get("field") or ""
            if name == "@ptr":
                continue
            value = field.get("value") or ""
            out[name] = truncate(value, self._limits.max_log_message_length)
        return out

    # ── Tool 3: get_vp_record ──────────────────────────────────────────────

    # Field-level caveats, returned with every record. These are properties of
    # how LMA writes the row, not of any particular failure, and a model that
    # does not know them reads the row wrongly in a specific and confident way.
    VP_FIELD_NOTES: Dict[str, str] = {
        "errorMessage": (
            "Prose, and deliberately overloaded: it carries JOINING sub-step "
            "progress as well as errors, so a non-empty value does not by "
            "itself mean the virtual participant failed. Read it together with "
            "status."
        ),
        "endTrigger": (
            "The structured exit code, written only on the terminal statuses "
            "COMPLETED and FAILED. This is the field to aggregate on; "
            "errorMessage is the prose shown to the user. A row that ended "
            "without one did not reach a terminal state through the container's "
            "own exit path."
        ),
        "status": (
            "The current status only. There is no status history on the row, so "
            "the order in which a join progressed has to come from the logs."
        ),
        "endReason": (
            "Why the virtual participant was ended from outside (by a user or a "
            "reaper), not why it exited on its own."
        ),
    }

    def get_vp_record(self, vp_id: Any) -> str:
        """Read one virtual participant row from DynamoDB.

        Args:
            vp_id: The virtual participant id.

        Returns:
            A JSON object with the row's attributes and the field-level caveats
            needed to read them correctly.
        """
        self._check_deadline("a virtual participant lookup")
        identifier = str(vp_id or "").strip()
        if not identifier:
            raise ToolError("no virtual participant id was supplied")
        if not self._vp_table_name:
            raise ToolError(
                "the virtual participant table is not configured for this "
                "function, so the row cannot be read"
            )

        response = self._ddb.get_item(
            TableName=self._vp_table_name,
            Key={"id": {"S": identifier}},
            ConsistentRead=True,
        )
        item = response.get("Item")
        if not item:
            return self._render(
                {
                    "vpId": identifier,
                    "found": False,
                    "guidance": (
                        "No row with this id. Either the id is wrong, or the row "
                        "has passed its TTL and expired. An expired row is not "
                        "evidence that the virtual participant never ran — the "
                        "launch state machine's log group outlives the row."
                    ),
                }
            )

        record = {name: _plain_value(value) for name, value in item.items()}
        notes = {k: v for k, v in self.VP_FIELD_NOTES.items() if k in record}
        return self._render(
            {
                "vpId": identifier,
                "found": True,
                "record": record,
                "fieldNotes": notes,
                "guidance": (
                    "This row is the current state, not a timeline. The ~28-value "
                    "exit taxonomy and the AI vision classifier's admission "
                    "verdicts are not persisted here; search the "
                    "virtual-participant log group for them."
                ),
            }
        )

    # ── Tool 4: get_stack_parameters ───────────────────────────────────────

    # Parameters that change which evidence exists, rather than merely how the
    # solution behaves. Reported first and annotated, because an agent that
    # does not read these looks for evidence in a component the deployment is
    # not running.
    ARCHITECTURE_PARAMETERS: Dict[str, str] = {
        "VPLaunchType": (
            "Decides where the virtual participant runs. Under MICROVM (the "
            "default) the Fargate log group is empty, no ECS task-stopped event "
            "is ever emitted, and there is no network-registration phase — the "
            "evidence is in the MicroVM log group instead. Under FARGATE or EC2 "
            "the opposite holds."
        ),
        "EnableMicrovmAsr": (
            "Whether the on-demand ASR engine is deployed at all. Even when "
            "true, a meeting is only transcribed by it once an admin moves its "
            "source onto it on the Transcription Engine page, so a deployment "
            "with this on may still be using Amazon Transcribe for every "
            "meeting."
        ),
        "VoiceAssistantProvider": "Which in-meeting voice assistant is running, if any.",
        "MeetingAssistService": (
            "Which assistant backend is deployed. Decides whether the "
            "meeting-assistant log group exists."
        ),
        "EnableAudioRecording": "Whether meeting audio is retained.",
        "EnableVideoRecording": "Whether meeting screen capture is retained.",
        "TranscribeLanguageCode": "Affects both engines' behaviour.",
        "IsSentimentAnalysisEnabled": "Affects the transcript pipeline's work per segment.",
        "MeetingInactivityTimeoutInMinutes": (
            "Zero removes the stale-meeting reaper's schedule entirely rather "
            "than changing a number, so a meeting stuck In Progress has no "
            "backstop."
        ),
    }

    def get_stack_parameters(self) -> str:
        """Read the deployment's CloudFormation parameters.

        Returns:
            A JSON object with the parameters that determine which components
            exist and where their evidence lives, then the remainder.
        """
        self._check_deadline("a stack parameter lookup")
        if not self._stack_name:
            raise ToolError("the LMA stack name is not configured for this function")

        response = self._cfn.describe_stacks(StackName=self._stack_name)
        stacks = response.get("Stacks") or []
        if not stacks:
            raise ToolError(f"stack {self._stack_name} was not found")
        stack = stacks[0]

        parameters = {
            p.get("ParameterKey"): p.get("ParameterValue")
            for p in stack.get("Parameters") or []
        }
        architecture = {
            key: parameters[key]
            for key in self.ARCHITECTURE_PARAMETERS
            if key in parameters
        }
        remainder = {
            key: value
            for key, value in sorted(parameters.items())
            if key not in architecture
        }
        return self._render(
            {
                "stackName": stack.get("StackName"),
                "stackStatus": stack.get("StackStatus"),
                "lastUpdatedTime": stack.get("LastUpdatedTime"),
                "architectureParameters": architecture,
                "architectureParameterNotes": {
                    key: note
                    for key, note in self.ARCHITECTURE_PARAMETERS.items()
                    if key in architecture
                },
                "otherParameters": remainder,
            }
        )


def _plain_value(attribute: Dict[str, Any]) -> Any:
    """Convert one DynamoDB wire-format attribute to a plain Python value.

    Only the types LMA's virtual participant rows actually use are handled.
    Anything else is rendered as its wire form rather than dropped, so an
    unexpected attribute type shows up in the diagnosis instead of vanishing
    from it.
    """
    if not isinstance(attribute, dict) or len(attribute) != 1:
        return attribute
    (kind, value), = attribute.items()
    if kind == "S":
        return value
    if kind == "N":
        return float(value) if re.search(r"[.eE]", str(value)) else int(value)
    if kind == "BOOL":
        return bool(value)
    if kind == "NULL":
        return None
    if kind == "L":
        return [_plain_value(v) for v in value]
    if kind == "M":
        return {k: _plain_value(v) for k, v in value.items()}
    if kind in ("SS", "NS", "BS"):
        return list(value)
    return attribute
