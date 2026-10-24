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
import logging
import re
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence

import troubleshooting_discovery as discovery
from troubleshooting_config import AgentLimits, truncate

LOGGER = logging.getLogger(__name__)

# Logs Insights accepts at most 50 log groups in one query.
_INSIGHTS_MAX_GROUPS = 50

# States that mean a Logs Insights query has stopped running. Only "Complete"
# means it produced an answer: "Timeout" and "Unknown" are failures that happen
# to return an empty result set, so they must not be read as "nothing matched".
_QUERY_DONE = frozenset({"Complete", "Failed", "Cancelled", "Timeout", "Unknown"})
_QUERY_SUCCEEDED = "Complete"

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


#: Error codes CloudWatch Logs returns for a query it would not accept. Read
#: from the error's own code rather than matched against its message, which is
#: not a stable interface.
_QUERY_SYNTAX_ERROR_CODES = frozenset(
    {
        "MalformedQueryException",
        "InvalidParameterException",
        "ValidationException",
    }
)


def _error_code(exc: BaseException) -> Optional[str]:
    """The AWS error code on `exc`, or None if it carries none."""
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return None
    return (response.get("Error") or {}).get("Code")


def _is_query_syntax_error(exc: BaseException) -> bool:
    """Whether `exc` means the query itself was rejected."""
    return _error_code(exc) in _QUERY_SYNTAX_ERROR_CODES


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
        # Cached across tool calls within one run: both answers are the same
        # every time and the model reliably asks for the log groups more than
        # once.
        self._discovered: Optional[discovery.DiscoveryResult] = None
        self._nested_names: Optional[List[str]] = None

    # ── Shared helpers ─────────────────────────────────────────────────────

    def _check_deadline(self, what: str) -> None:
        if self._deadline is not None and self._monotonic() >= self._deadline:
            raise ToolError(
                f"the time budget for this diagnosis was exhausted before {what} "
                "could run; report what has been established so far and name "
                "this as the evidence that is missing"
            )

    def _render(
        self,
        payload: Dict[str, Any],
        *,
        bulk_key: Optional[str] = None,
        shed_count_key: str = "itemsOmittedForSize",
    ) -> str:
        """Serialise a tool result, kept under the configured size.

        Oversize results shed trailing items from ``bulk_key`` — a list or a
        dict — rather than having the serialised string cut at the end.
        Cutting the string is the obvious implementation and it is wrong twice
        over:

        * The fields the agent is *told* to read — ``guidance``, the
          completeness flags, the caveats — are small and are serialised after
          the bulk array, so they are the first thing a tail cut removes. The
          warning saying "these log groups were not searched" would be lost in
          exactly the case that produces it.
        * A cut string is no longer valid JSON, so every reader downstream
          (including the thinking-step summariser) falls back to knowing
          nothing about the result.

        Shedding items keeps the payload valid and keeps the small fields,
        which is the opposite priority to a tail cut and the right one: 40 rows
        and an accurate caveat beat 100 rows and no caveat.
        """
        cap = self._limits.max_tool_result_chars

        def serialise(body: Dict[str, Any]) -> str:
            return json.dumps(body, default=str, indent=None)

        rendered = serialise(payload)
        items = payload.get(bulk_key) if bulk_key else None
        if len(rendered) <= cap or not isinstance(items, (list, dict)) or not items:
            # No bulk collection to shed from. truncate() is the last resort,
            # and its marker is what tells a reader the result is unparseable.
            return truncate(rendered, cap)

        # A dict bulk collection (a parameter map) sheds by key, a list (rows,
        # log groups) by position. Both keep a prefix, so what remains is a
        # coherent subset rather than a sample.
        is_mapping = isinstance(items, dict)
        order: List[Any] = list(items.keys()) if is_mapping else list(items)

        def take(n: int) -> Any:
            if is_mapping:
                return {k: items[k] for k in order[:n]}
            return order[:n]

        def attempt(n: int) -> str:
            trial = dict(payload)
            trial[bulk_key] = take(n)
            trial[shed_count_key] = len(order) - n
            trial["guidance"] = " ".join(
                part
                for part in (
                    payload.get("guidance"),
                    f"{len(order) - n} of {len(order)} items were removed from "
                    "this result because it was too large to return in full. "
                    "What remains is accurate; ask a narrower question for the "
                    "rest.",
                )
                if part
            )
            return serialise(trial)

        # Binary search for the longest prefix that fits, rather than halving
        # until it does. Both cost about log2(N) serialisations, and this one
        # keeps as much evidence as the cap allows: halving would drop 50 of
        # 100 rows for a result one character over.
        low, high, best = 0, len(order), None
        while low <= high:
            middle = (low + high) // 2
            rendered = attempt(middle)
            if len(rendered) <= cap:
                best = rendered
                low = middle + 1
            else:
                high = middle - 1

        if best is not None:
            return best

        # Even the small fields alone are over the cap, which means a caller
        # configured an unusably small limit. Say so rather than return a
        # fragment of the array.
        minimal = {k: v for k, v in payload.items() if k != bulk_key}
        minimal[shed_count_key] = len(order)
        return truncate(serialise(minimal), cap)

    def _known_log_group_names(self) -> set[str]:
        return {g.name for g in self._discovery().groups}

    def _nested_stack_names(self) -> List[str]:
        """This deployment's nested stacks' generated physical names.

        Resolved rather than inferred from the parent's name, because the
        obvious inference is wrong in a way that is hard to notice: a log group
        whose name begins ``/<parent>-`` belongs to *a* stack whose name begins
        with the parent's, which in an account holding both ``LMA`` and
        ``LMA-dev`` is not necessarily this one. Reporting a neighbouring
        deployment's log line as this deployment's evidence is the worst answer
        this agent could give, so the names are looked up.

        Returns an empty list when the lookup is not possible, and the caller
        says so in its result rather than quietly falling back.
        """
        if self._nested_names is not None:
            return self._nested_names
        names: List[str] = []
        try:
            paginator = self._cfn.get_paginator("list_stack_resources")
            for page in paginator.paginate(StackName=self._stack_name):
                for resource in page.get("StackResourceSummaries", []):
                    if resource.get("ResourceType") != "AWS::CloudFormation::Stack":
                        continue
                    physical_id = resource.get("PhysicalResourceId") or ""
                    # The physical id of a nested stack is its ARN; the stack
                    # name is the second-to-last segment.
                    parts = physical_id.split("/")
                    if len(parts) >= 2 and parts[-2]:
                        names.append(parts[-2])
        except Exception:  # noqa: BLE001 - reported, not raised
            LOGGER.warning(
                "could not resolve nested stack names for %s; log group "
                "discovery will match by stack-name prefix",
                self._stack_name,
            )
            names = []
        self._nested_names = names
        return names

    def _discovery(self) -> discovery.DiscoveryResult:
        """One discovery pass per run, cached.

        Cached because the answer cannot change inside one diagnosis and the
        model reliably asks for it more than once — and because every
        ``search_logs`` call validates its arguments against it, so an uncached
        lookup would re-page ``describe_log_groups`` on every query.
        """
        if self._discovered is None:
            self._discovered = discovery.discover_log_groups(
                self._logs,
                self._stack_name,
                max_log_groups=self._limits.max_log_groups,
                graphql_url=self._graphql_url,
                nested_stack_names=self._nested_stack_names() or None,
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
        result = self._discovery()
        wanted = _as_term_list(components) or None
        groups = result.groups
        if wanted is not None:
            allowed = set(wanted)
            groups = [g for g in groups if g.component in allowed]

        present = [g for g in groups if g.has_data]
        empty = [g for g in groups if not g.has_data]
        guidance = [
            f"{len(present)} of {len(groups)} groups hold data. A group with "
            "hasData=false has never been written to, which means this "
            "deployment does not use that path — it is not evidence that the "
            "component failed. Query only the groups that hold data."
        ]
        # Caveats come after the reading advice and before anything else the
        # model does: each one names a way this list is incomplete or wider
        # than this deployment, and reasoning past either produces a confident
        # wrong answer rather than a visible gap.
        guidance.extend(result.caveats())
        # Key order is load-bearing: everything the agent must read sits ahead
        # of the bulk array, so it survives even the last-resort tail cut.
        return self._render(
            {
                "stackName": self._stack_name,
                "logGroupCount": len(groups),
                "emptyLogGroupCount": len(empty),
                "listIsComplete": not result.truncated,
                "scopedToThisDeploymentOnly": result.scoped_to_nested_stacks,
                "guidance": " ".join(guidance),
                "componentNotes": {
                    c: discovery.COMPONENT_NOTES[c]
                    for c in sorted({g.component for g in groups})
                    if c in discovery.COMPONENT_NOTES
                },
                "logGroups": [g.to_dict() for g in groups],
            },
            bulk_key="logGroups",
            shed_count_key="logGroupsOmittedForSize",
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
        # Logs Insights takes at most 50 groups in one query. The ones past
        # that are reported rather than dropped quietly: a group the model
        # asked about and did not get searched would otherwise look like a
        # group that held nothing, which is the confusion this whole module is
        # built to avoid.
        allowed = min(_INSIGHTS_MAX_GROUPS, self._limits.max_log_groups)
        omitted = groups[allowed:]
        groups = groups[:allowed]

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

        guidance: List[str] = []
        if not rows:
            guidance.append(
                "An empty rowCount means no line in this window matched. That "
                "is not a cause; it narrows where the cause can be. Widen "
                "lookbackHours, drop a term, or query a different component "
                "before concluding anything from it."
            )
        if unknown:
            guidance.append(
                f"{len(unknown)} name(s) you gave are not log groups in this "
                "deployment and were NOT searched: see unknownLogGroupNames. "
                "Nothing here says anything about them. Call "
                "discover_log_groups and use the names it returns verbatim."
            )
        if omitted:
            guidance.append(
                f"{len(omitted)} log group(s) you named were NOT searched, "
                "because one query can cover at most "
                f"{_INSIGHTS_MAX_GROUPS}: see omittedLogGroupNames. Nothing "
                "here says anything about those groups. Query them separately."
            )
        # Key order is load-bearing: see _render. `rows` is last so it is what
        # gets shed, and `guidance` is ahead of it so a caveat is never the
        # thing that falls off the end.
        return self._render(
            {
                "guidance": " ".join(guidance) if guidance else None,
                "status": status,
                "rowCount": len(rows),
                "logGroupCount": len(groups),
                "logGroupNames": groups,
                "unknownLogGroupNames": unknown,
                "omittedLogGroupNames": list(omitted),
                "queryString": resolved_query,
                "lookbackHours": hours,
                "bytesScanned": scanned,
                "rows": rows,
            },
            bulk_key="rows",
            shed_count_key="rowsOmittedForSize",
        )

    def _resolve_limit(self, limit: Any) -> int:
        maximum = self._limits.max_log_events
        if limit is None:
            return maximum
        try:
            value = int(float(limit))
        except (TypeError, ValueError, OverflowError):
            # OverflowError is int(float("inf")); see resolve_lookback_hours.
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

        **The wait is bounded by the run's deadline as well as by
        ``query_timeout_seconds``.** Checking only the per-query timeout lets a
        query started just inside the deadline run on past it, and the model
        still has to write an answer after this returns — so a query that
        cannot finish in the time the run has left is cancelled at that point
        instead.
        """
        try:
            started = self._logs.start_query(
                logGroupNames=list(log_group_names),
                startTime=start_time,
                endTime=end_time,
                queryString=query_string,
                limit=row_limit,
            )
        except Exception as exc:  # noqa: BLE001 - classified, then re-raised
            # A query the model wrote can be rejected outright, and that is
            # the model's problem to fix rather than a fault in this tool.
            # Letting it reach the generic handler would label it "a fault in
            # the tool, not evidence about LMA" and leave the agent with no
            # next step, when the next step is to correct the query.
            if _error_code(exc) == "AccessDeniedException":
                # Discovery can list a group this role cannot query — under
                # the prefix-matching fallback, or an AppSync group found from
                # the endpoint on a deployment that supplied no API id. The
                # generic handler would call that "a fault in the tool, not
                # evidence about LMA" and leave the agent stuck; it is a scope
                # problem it can work around.
                raise ToolError(
                    "this deployment's diagnostic role is not permitted to "
                    f"query at least one of these log groups: {exc}. Retry "
                    "with fewer groups, dropping any that discovery reported "
                    "but that may belong outside this deployment."
                ) from exc
            if _is_query_syntax_error(exc):
                raise ToolError(
                    "CloudWatch Logs rejected this query as malformed: "
                    f"{exc}. The query is yours to fix — check the pipe "
                    "syntax, the field names and the quoting, then try again. "
                    "Omit queryString and pass search_terms instead if you "
                    "only need matching lines."
                ) from exc
            raise

        query_id = started.get("queryId")
        if not query_id:
            raise ToolError("CloudWatch Logs did not return a query id")

        budget = float(self._limits.query_timeout_seconds)
        if self._deadline is not None:
            budget = min(budget, self._deadline - self._monotonic())

        # Measured against the clock, not accumulated from the sleep intervals.
        # Each get_query_results call takes time of its own, so summing the
        # sleeps undercounts by the poll count times the API latency — and this
        # figure is compared against a budget derived from the run deadline,
        # which is real time.
        started_at = self._monotonic()
        while True:
            response = self._logs.get_query_results(queryId=query_id)
            status = response.get("status", "Unknown")
            if status in _QUERY_DONE:
                break
            waited = self._monotonic() - started_at
            if waited >= budget:
                self._stop_query(query_id)
                raise ToolError(
                    f"the query was still {status} after {waited:.0f}s of an "
                    f"available {budget:.0f}s and was cancelled. Narrow it: "
                    "fewer log groups, a shorter lookbackHours, or a more "
                    "specific search term."
                )
            self._sleep(self._limits.query_poll_seconds)

        if status != _QUERY_SUCCEEDED:
            # Anything other than Complete, not just Failed and Cancelled. A
            # query that ended as Timeout or Unknown returns an empty result
            # set, and returning that as rowCount 0 would hand the agent "no
            # line matched" for what is actually "the query did not run" —
            # the exact conflation this module's ToolError exists to prevent.
            raise ToolError(
                f"the Logs Insights query ended as {status} rather than "
                "Complete, so it produced no result. This is not evidence that "
                "nothing matched. Retry it, or narrow it first if it was large."
            )

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

    #: The attributes of a virtual participant row this tool will return.
    #:
    #: An allowlist, not a denylist, and that direction is the point: the row
    #: carries a meeting password and a VNC password, and whatever this tool
    #: returns goes into the model's context *and* onto the thinking-step
    #: stream the UI renders. A field added to the row later is withheld until
    #: somebody adds it here on purpose, which is the safe way for this list to
    #: be out of date.
    #:
    #: The prompt also tells the agent to redact credentials from the evidence
    #: it quotes. That instruction is worth keeping, but it is a request to a
    #: model and cannot be the control: this list is.
    DIAGNOSTIC_FIELDS = frozenset(
        {
            # Identity of the attempt and what it was trying to join. The
            # meeting name and id are here because a failure that reproduces
            # for one meeting name and not others is a documented pattern.
            "id",
            "meetingName",
            "meetingPlatform",
            "meetingId",
            "CallId",
            # Lifecycle.
            "status",
            "errorMessage",
            "endTrigger",
            "endReason",
            "userAcknowledgedFailure",
            "statusBeforeManualAction",
            # Timing.
            "meetingTime",
            "scheduledFor",
            "isScheduled",
            "createdAt",
            "updatedAt",
            # The manual-action interruption, whose rate is itself a signal.
            "manualActionType",
            "manualActionMessage",
            "manualActionTimeoutSeconds",
            "manualActionStartTime",
            # Live-view reachability. Endpoint and port, never the password.
            "vncEndpoint",
            "vncPort",
            "vncReady",
            "taskPrivateIp",
            # Per-VP toggles that change what evidence exists.
            "enableVideoRecording",
        }
    )

    def get_vp_record(self, vp_id: Any) -> str:
        """Read one virtual participant row from DynamoDB.

        Only the attributes in DIAGNOSTIC_FIELDS are returned. The names of the
        rest are reported so the agent knows the record was filtered and a
        reader can see what was held back, but their values never leave here.

        Args:
            vp_id: The virtual participant id.

        Returns:
            A JSON object with the row's diagnostic attributes and the
            field-level caveats needed to read them correctly.
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

        record = {
            name: _plain_value(value)
            for name, value in item.items()
            if name in self.DIAGNOSTIC_FIELDS
        }
        withheld = sorted(set(item) - self.DIAGNOSTIC_FIELDS)
        notes = {k: v for k, v in self.VP_FIELD_NOTES.items() if k in record}
        return self._render(
            {
                "vpId": identifier,
                "found": True,
                "record": record,
                "withheldFields": withheld,
                "fieldNotes": notes,
                "guidance": (
                    "This row is the current state, not a timeline. The ~28-value "
                    "exit taxonomy and the AI vision classifier's admission "
                    "verdicts are not persisted here; search the "
                    "virtual-participant log group for them. The attributes named "
                    "in withheldFields exist on the row but are not returned, "
                    "because they are credentials or identify a person rather "
                    "than describing the failure; do not ask for them and do not "
                    "treat their absence as a finding."
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
        "MeetingInactivityTimeoutInMinutes": (
            "Zero removes the stale-meeting reaper's schedule entirely rather "
            "than changing a number, so a meeting stuck In Progress has no "
            "backstop."
        ),
    }

    #: Parameter names matching any of these are reported by name only.
    #:
    #: Defence in depth, not the control. CloudFormation already masks a
    #: ``NoEcho`` parameter in DescribeStacks output, and all five of this
    #: solution's third-party credentials are declared that way, so a secret
    #: arrives here as asterisks rather than as itself. This catches two
    #: remaining cases: a parameter that carries an address or a name, which is
    #: personal data a diagnosis has no use for; and a future credential
    #: parameter that someone forgets to mark NoEcho.
    REDACTED_PARAMETER_PATTERNS = (
        "password",
        "secret",
        "apikey",
        "api_key",
        "token",
        "credential",
        "connectionstring",
        "privatekey",
        "email",
        "address",
    )

    @classmethod
    def _is_redacted_parameter(cls, name: str) -> bool:
        lowered = (name or "").lower()
        return any(pattern in lowered for pattern in cls.REDACTED_PARAMETER_PATTERNS)

    def get_stack_parameters(self) -> str:
        """Read the deployment's CloudFormation parameters.

        Parameter *values* are configuration the agent needs, so they are
        returned — with the exception of anything whose name suggests a
        credential or personal data, which is reported by name only.

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

        declared = {
            p.get("ParameterKey"): p.get("ParameterValue")
            for p in stack.get("Parameters") or []
            if p.get("ParameterKey")
        }
        redacted = sorted(k for k in declared if self._is_redacted_parameter(k))
        parameters = {k: v for k, v in declared.items() if k not in redacted}

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
                "redactedParameters": redacted,
                "guidance": (
                    "The parameters named in redactedParameters have values on "
                    "this stack that are not returned, because the name "
                    "indicates a credential or personal data. Whether one is "
                    "set is usually all a diagnosis needs; if the value itself "
                    "matters, ask the operator to check it."
                )
                if redacted
                else None,
                # Last, and the bulk collection, for the same reason rows are
                # in search_logs: an oversize result sheds from here rather
                # than losing the annotated architecture parameters or the
                # redaction note.
                "otherParameters": remainder,
            },
            bulk_key="otherParameters",
            shed_count_key="otherParametersOmittedForSize",
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
