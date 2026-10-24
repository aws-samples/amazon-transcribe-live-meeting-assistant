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
import math
import re
import time
from datetime import datetime, timezone
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

    ``summary`` is a short label written here, safe to publish on the
    thinking-step stream. The message itself is not: several of these embed a
    botocore exception's text, which can carry account-specific ARNs, or a log
    group name the model supplied, which is model text. The message goes to the
    agent, which needs the detail to act; the summary goes to the UI panel,
    which needs to know only what kind of thing went wrong.
    """

    def __init__(self, message: str, summary: str = "could not gather evidence"):
        super().__init__(message)
        self.summary = summary


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


#: VP statuses after which the participant does not run again.
_VP_TERMINAL_STATUSES = frozenset({"COMPLETED", "FAILED", "ENDED", "CANCELLED"})

#: The literal the container writes when it leaves through its own exit path.
_VP_EXIT_MARKER = "VP exit:"

#: Literals a participant writes when it is stopped by a signal rather than
#: leaving on its own. The supervisor line is MicroVM-only; the other two come
#: from the container's signal handler on every launch type. Only the last
#: can carry the VP id ("VP <id> status updated to COMPLETED due to external
#: termination", written once the status update succeeds); for the others the
#: stream they are written to is what ties them to the participant.
_VP_EXTERNAL_STOP_MARKERS = (
    "Received SIGTERM",
    "[terminate] signalling app to exit",
    "due to external termination",
)


#: Attributes only the end operation writes. ``endedAt`` is not one of them:
#: the VP task reaper writes it too.
_END_OPERATION_FIELDS = ("endReason", "ecsTerminated", "kinesisEndSent", "scheduleCancelled")


def _vp_end_search_terms(vp_id: str) -> str:
    """The one search that answers "did it join, and how did it end".

    The id matches the participant's own status lines, which show the join
    progress and identify its stream; the stop markers and the exit line match
    whichever way it ended. Terms are ORed, so this is one tool call where
    searching for them in turn would be five.
    """
    return ", ".join((vp_id, *_VP_EXTERNAL_STOP_MARKERS, _VP_EXIT_MARKER))


def _hours_since(timestamp: Any, now: float) -> Optional[int]:
    """Whole hours, rounded up, from an ISO-8601 timestamp to ``now``.

    None for anything that does not parse, so a row with an odd timestamp
    gets no lookback advice rather than wrong advice.
    """
    if not isinstance(timestamp, str) or not timestamp.strip():
        return None
    try:
        parsed = datetime.fromisoformat(timestamp.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    elapsed = now - parsed.timestamp()
    if elapsed < 0:
        return None
    return max(1, math.ceil(elapsed / 3600))


def assess_vp_end(
    vp_id: str,
    record: Dict[str, Any],
    *,
    now: Optional[float] = None,
    max_lookback_hours: Optional[int] = None,
) -> str:
    """Say what a virtual participant row records about how it ended.

    This reads the row the way the code that writes it does, so the model does
    not have to infer it. Four writers set the end-related fields, and each
    leaves a different shape:

    * The end operation (``endVirtualParticipant`` from the UI, the SDK or
      CLI, or a delete) sets ``endedAt``, ``endReason`` and status ENDED or
      CANCELLED, plus ``ecsTerminated``/``kinesisEndSent`` or
      ``scheduleCancelled``, then stops the host. The participant is
      signalled and logs the stop markers, never a ``VP exit:`` line.
    * The VP task reaper (ECS launch types only, on a task-stopped event)
      sets FAILED, ``endedAt`` and an ``errorMessage`` carrying the ECS stop
      code and reason, and no ``endReason``. The container may have been
      killed outright, so it may have logged nothing at the end.
    * The container's own exit path sets COMPLETED or FAILED with an
      ``endTrigger``, and logs ``VP exit:``.
    * The container's signal handler sets COMPLETED with neither, when it is
      stopped by something that did not go through the end operation.

    So ``endedAt`` alone does not identify the end operation: the fields only
    it writes do. The participant also writes its own progress statuses while
    it starts up, and one of those can land after the end operation's write.
    The row then shows a non-terminal status alongside ``endedAt``, which
    reads as "still running" unless something says otherwise.

    The suggested search carries a ``lookback_hours`` reaching back to the
    row's ``createdAt``. search_logs only searches back from now, and its
    default window is a few hours, so the same search run against a
    participant from this morning returns nothing and reads as "no
    evidence" — which is how a run spends its remaining calls widening the
    window one guess at a time.
    """
    status = str(record.get("status") or "")
    # Presence, not truthiness: the end operation writes endReason even when
    # the caller gave an empty one, and these fields are written by nothing
    # else.
    ended_by_operation = any(name in record for name in _END_OPERATION_FIELDS)
    reaped = not ended_by_operation and bool(record.get("endedAt"))
    end_trigger = record.get("endTrigger")
    window = ""
    hours = _hours_since(record.get("createdAt"), time.time() if now is None else now)
    if hours is not None:
        # One hour of margin either side of a clock or rounding edge.
        hours += 1
        if max_lookback_hours is not None and hours > max_lookback_hours:
            window = (
                f" and lookback_hours {max_lookback_hours} (the most allowed; "
                "this participant was created before that window, so its "
                "earliest lines may be out of reach)"
            )
        else:
            window = (
                f" and lookback_hours {hours}, which reaches back to its "
                "createdAt (the default window may not)"
            )
    search = (
        "To corroborate this, and to see whether it joined first, run ONE "
        "search_logs over the virtual-participant log groups with search_terms "
        f'"{_vp_end_search_terms(vp_id)}"{window}. The lines carrying the id '
        "show how far the join progressed (for example a status update to "
        "JOINED) and name the participant's @logStream; the shutdown lines in "
        "that same stream show how it stopped. Do not search for each term in "
        "a separate call."
    )

    # No row value is interpolated into these sentences. endReason and
    # errorMessage are text written by callers, status is a free-form string
    # on the row, and these sentences are the tool's own voice; the record
    # below is where the model reads and quotes them, as evidence.
    stale = (
        "Its status (see the record) is not a terminal one even though the end "
        "is recorded, so the status field is stale: a progress update the "
        "participant wrote as it was shutting down landed after the end "
        "(compare updatedAt with endedAt). Treat the participant as ended, and "
        "say in your answer that the status shown is stale."
    )

    if reaped:
        parts = [
            "endedAt is set with no endReason: that is the shape the VP task "
            "reaper leaves when ECS reports the participant's task stopped "
            "(FARGATE and EC2 launch types). It sets FAILED and writes the ECS "
            "stop code and stopped reason into errorMessage, so errorMessage "
            "is the recorded reason it ended: quote it, with endedAt. A host "
            "restarted for maintenance appears there as "
            "EssentialContainerExited."
        ]
        if status not in _VP_TERMINAL_STATUSES:
            parts.append(stale)
        parts.append(
            "The container may have been killed without logging a shutdown, "
            "so an absent exit line or stop marker is expected here; its last "
            "lines show what it was doing when it stopped. " + search
        )
        return " ".join(parts)

    if ended_by_operation:
        parts = [
            "This row records that the participant was ended from outside, by "
            "the end operation: see endedAt and endReason in the record. That "
            "is the primary evidence of why it ended: report it as the reason, "
            "quoting endReason and endedAt, rather than as something still to "
            "be explained."
        ]
        if status not in _VP_TERMINAL_STATUSES:
            parts.append(stale)
        parts.append(
            "A participant stopped from outside does not write a "
            f"'{_VP_EXIT_MARKER}' line, so do not look for one; it writes "
            + ", ".join(f"'{m}'" for m in _VP_EXTERNAL_STOP_MARKERS)
            + " instead."
        )
        parts.append(search)
        return " ".join(parts)

    if status in ("COMPLETED", "FAILED") and end_trigger:
        return (
            "The participant left through its own exit path: see endTrigger "
            f"in the record. Its '{_VP_EXIT_MARKER}' line in the "
            "virtual-participant log group carries the same trigger together "
            "with reason=, and the lines before it explain the trigger. "
            + search
        )

    if status == "COMPLETED":
        return (
            "COMPLETED with no endTrigger and no endReason is what the "
            "participant's signal handler writes: it was stopped by a signal "
            "that did not come through the end operation, such as its host "
            f"shutting down, so there is no '{_VP_EXIT_MARKER}' line. " + search
        )

    if status in _VP_TERMINAL_STATUSES:
        return (
            f"The row is in the terminal status {status} but records no "
            "endTrigger, endReason or endedAt. " + search
        )

    return (
        "No end is recorded on this row. If the participant is no longer "
        "running, it stopped without its own exit path or the end operation "
        "writing here, for example because its host went away, so look for its "
        "last log lines rather than only for an exit line. " + search
    )


def _empty_result_guidance(
    resolved_query: str, terms: Sequence[str], hours: int, max_hours: int
) -> List[str]:
    """What to do next after a search that matched nothing.

    Written for the ways a run actually runs out of tool calls: a window that
    did not reach back far enough, and the same kind of search retried one
    term at a time, each retry spending a call. So the advice names the window
    that was searched, says to broaden within one call, and, for the one
    marker whose absence is most often misread, says what is written instead.
    """
    if hours < max_hours:
        window = (
            f"This search covered only the last {hours} hour(s); if the event "
            "could be older (compare a record's createdAt with now), pass "
            f"lookback_hours to reach it, up to {max_hours}."
        )
    else:
        window = f"This search covered the last {hours} hour(s), the most allowed."
    guidance = [
        "An empty rowCount means no line in this window matched. That is not a "
        f"cause; it narrows where the cause can be. {window} Otherwise query a "
        "different component, or add the alternative terms you would try next "
        "to the same call (search_terms are ORed, so each extra term can only "
        "add lines), before concluding anything from it. Each search spends one "
        "of a small, fixed number of tool calls, so do not retry one term at a "
        "time."
    ]
    if _VP_EXIT_MARKER in resolved_query and not any(
        marker in resolved_query for marker in _VP_EXTERNAL_STOP_MARKERS
    ):
        guidance.append(
            f"No '{_VP_EXIT_MARKER}' line is not the same as no ending: that "
            "line is written only when the participant leaves through its own "
            "exit path. One stopped from outside, by the end operation or by "
            "its host shutting down, writes "
            + ", ".join(f"'{m}'" for m in _VP_EXTERNAL_STOP_MARKERS)
            + " instead, and one whose task was killed outright may write "
            "neither. Check get_vp_record for endReason, endedAt and "
            "errorMessage, and search for those lines together with the VP id "
            "in one call."
        )
    elif len(terms) == 1:
        guidance.append(
            "This search had a single term. If you are looking for one "
            "participant or meeting, include its id as another term in the "
            "same call."
        )
    return guidance


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
        appsync_api_id: This deployment's AppSync API id, used to find the
            service-managed AppSync log group.
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
        appsync_api_id: str = "",
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
        self._appsync_api_id = appsync_api_id
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
                "this as the evidence that is missing",
                summary="out of time",
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
                appsync_api_id=self._appsync_api_id,
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
            f"{len(present)} of {len(groups)} groups report stored data. "
            "hasData=false means CloudWatch stores no bytes for that group "
            "RIGHT NOW. That is a hint, not a fact about whether the path is "
            "used: a group also reports false when every event in it has aged "
            "past the retention period, and briefly when its first events are "
            "very recent. So do NOT skip a group for it — a participant idle "
            "for a fortnight and failing today reports false on the very group "
            "holding the failure. Use it to rank where to look, and read "
            "get_stack_parameters to find the groups this deployment genuinely "
            "does not use, which is a question about configuration rather than "
            "about bytes."
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
            lookback_hours: How far back to search, in hours.
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
                "cannot be guessed from the stack name.",
                summary="no log group named",
            )

        known = self._known_log_group_names()
        groups = [name for name in requested if name in known]
        unknown = [name for name in requested if name not in known]
        if not groups:
            raise ToolError(
                "none of the named log groups exist in this deployment: "
                f"{unknown}. Call discover_log_groups and use the names it "
                "returns verbatim.",
                summary="named log groups do not exist in this deployment",
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

        terms: List[str] = []
        if isinstance(query_string, str) and query_string.strip():
            resolved_query = query_string.strip()
        else:
            terms = _as_term_list(search_terms)
            resolved_query = build_search_query(terms, row_limit)

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
            guidance.extend(
                _empty_result_guidance(
                    resolved_query, terms, hours, self._limits.max_lookback_hours
                )
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
                # Discovery can list a group this role cannot query: when the
                # nested stack names could not be resolved it matches by
                # stack-name prefix, which can reach a neighbouring
                # deployment's groups. The generic handler would call that "a
                # fault in the tool, not evidence about LMA" and leave the
                # agent stuck; it is a scope problem it can work around.
                raise ToolError(
                    "this deployment's diagnostic role is not permitted to "
                    f"query at least one of these log groups: {exc}. Retry "
                    "with fewer groups, dropping any that discovery reported "
                    "but that may belong outside this deployment.",
                    summary="not permitted to query one of those log groups",
                ) from exc
            if _is_query_syntax_error(exc):
                raise ToolError(
                    "CloudWatch Logs rejected this query as malformed: "
                    f"{exc}. The query is yours to fix — check the pipe "
                    "syntax, the field names and the quoting, then try again. "
                    "Omit query_string and pass search_terms instead if you "
                    "only need matching lines.",
                    summary="the query was rejected as malformed",
                ) from exc
            raise

        query_id = started.get("queryId")
        if not query_id:
            raise ToolError(
                "CloudWatch Logs did not return a query id",
                summary="the query could not be started",
            )

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
                    "fewer log groups, a shorter lookback_hours, or a more "
                    "specific search term.",
                    summary="the query was cancelled for taking too long",
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
                "nothing matched. Retry it, or narrow it first if it was large.",
                summary="the query did not complete",
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
            "the order in which a join progressed has to come from the logs. It "
            "is also the last write, not necessarily the last event: when "
            "endedAt is set the participant has ended even if status is not "
            "terminal."
        ),
        "endReason": (
            "Why the virtual participant was ended from outside, through the "
            "end operation (a user, the SDK or CLI, or a delete), not why it "
            "exited on its own. When present it is the recorded answer to why "
            "the participant ended, written by the operation that ended it."
        ),
        "endedAt": (
            "When the participant was ended from outside. Two writers set it: "
            "the end operation, together with endReason; and the VP task "
            "reaper, together with FAILED and an errorMessage giving the ECS "
            "stop reason, with no endReason. Either way the participant ended "
            "at this time whatever status says."
        ),
        "ecsTerminated": (
            "Whether the end operation stopped the participant's host. Named "
            "for ECS but set for a MicroVM host too."
        ),
        "kinesisEndSent": (
            "Whether the end operation sent the meeting's END event, which is "
            "what closes the transcript."
        ),
    }

    #: The attributes of a virtual participant row this tool will return.
    #:
    #: An allowlist, not a denylist, and that direction is the point: the row
    #: carries a meeting password and a VNC password, and whatever this tool
    #: returns reaches the model's context and the caller's response body. (It
    #: does not reach the thinking-step stream, which publishes only "record
    #: found" or "no record" — but the model's context and the response are
    #: reason enough.) A field added to the row later is withheld until
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
            # Written by the end operation alongside endReason; endedAt is also
            # written by the VP task reaper. They are how a row whose status
            # was overwritten after the end still shows that the participant
            # was ended, and whether its host and its transcript were closed
            # down.
            "endedAt",
            "ecsTerminated",
            "kinesisEndSent",
            "albCleanedUp",
            "scheduleCancelled",
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
            raise ToolError(
                "no virtual participant id was supplied",
                summary="no virtual participant id",
            )
        if not self._vp_table_name:
            raise ToolError(
                "the virtual participant table is not configured for this "
                "function, so the row cannot be read",
                summary="the virtual participant table is not configured",
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
        # endAssessment sits ahead of the record: it is what the model should
        # read first, and the record is the evidence it summarises.
        return self._render(
            {
                "vpId": identifier,
                "found": True,
                "endAssessment": assess_vp_end(
                    identifier,
                    record,
                    max_lookback_hours=self._limits.max_lookback_hours,
                ),
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
            raise ToolError(
                "the LMA stack name is not configured for this function",
                summary="the stack name is not configured",
            )

        response = self._cfn.describe_stacks(StackName=self._stack_name)
        stacks = response.get("Stacks") or []
        if not stacks:
            raise ToolError(
                f"stack {self._stack_name} was not found",
                summary="the stack was not found",
            )
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
