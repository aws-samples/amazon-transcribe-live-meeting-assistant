#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""Operator-tunable limits for the troubleshooting agent.

WHY THIS IS A MODULE OF ITS OWN
-------------------------------
Every number in here is a spend ceiling, and spend ceilings have to be
readable in one place and changeable without editing agent logic. They are
also the difference between an agent that answers in a minute and one that
reads a day of container logs into a 300-second Lambda timeout.

Three rules this module exists to enforce:

1. **A limit is code, not prompt text.** Investigative discipline ("prefer two
   independent sources") belongs in the system prompt. A cost control does
   not: a model that is asked nicely to stop at eight tool calls will
   sometimes make nine. Everything here is checked by the caller.
2. **Every limit has a hard ceiling the operator cannot raise.** The
   environment variables let a deployment tune *down* freely and tune *up*
   only to a bound set here, so a mistyped value degrades cost rather than
   exhausting the Lambda timeout or the Logs Insights concurrency quota. A
   value that is clamped, or that could not be read at all, is logged — see
   ``_read``.
3. **There is no whole-run retry, and this module deliberately offers no knob
   for one.** Restarting an agent run from the original question re-pays for
   every tool call it already made. A run that failed should surface the
   failure; the operator can ask again if they want to.

The wall-clock budget is the backstop for the other two. `max_iterations`
cannot bound a run on its own, because a single Logs Insights query can
legitimately take 45 seconds, and `token_budget` cannot either, because a
stalled HTTP call burns time without burning tokens.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field, fields
from typing import Any, Dict, Tuple

LOGGER = logging.getLogger(__name__)

# ── Hard ceilings ───────────────────────────────────────────────────────────
#
# (default, maximum). The maximum is the value an operator cannot exceed; a
# larger setting is clamped and logged rather than rejected, because a
# troubleshooting agent that refuses to start because a limit was typed too
# high is less useful than one that starts with a sane limit.

_LIMITS: Dict[str, Tuple[int, int]] = {
    # Tool calls, counted individually — so a single turn that calls three
    # tools in parallel spends three. Four tools need far fewer than this; the
    # headroom is for a follow-up query after a first one returns nothing.
    "max_iterations": (8, 20),
    # Output tokens per model turn. Diagnoses are short by contract (the
    # prompt's output section caps them), so this is generous already.
    "max_output_tokens": (4096, 16384),
    # Cumulative input + output tokens for the whole run, counted from the
    # usage figures Bedrock returns. The system prompt is large and resent on
    # every turn, which is what makes a per-turn cap insufficient.
    "token_budget": (250_000, 1_000_000),
    # Wall clock for the whole run. This is a *requested* budget: the effective
    # one is the smaller of this and the time the Lambda invocation actually
    # has left, minus a reserve for the final model turn — see
    # RunBudget.__init__. That indirection is deliberate. The function's
    # Timeout is a template value an operator can raise or lower, so a ceiling
    # written here could not stay inside it; measuring the remaining time
    # instead keeps the agent able to report "I ran out of time" rather than
    # being killed mid-answer, whatever Timeout is set to.
    "wall_clock_seconds": (240, 870),
    # Held back from the wall clock for the model to write its answer in once
    # evidence gathering stops. The answer is short by contract, but it is a
    # full model turn and it happens after the last tool returns.
    "answer_reserve_seconds": (30, 120),
    # ── Per-tool result caps ───────────────────────────────────────────────
    # Log groups returned by discovery. An LMA stack has ~70; a value below
    # that truncates discovery, which is why the default is above it.
    "max_log_groups": (120, 400),
    # Rows returned by one Logs Insights query. Logs Insights' own API
    # maximum is 10,000; the point of a lower cap is the model's context, not
    # the API's.
    "max_log_events": (100, 1000),
    # Characters per log message. VP container lines carry serialised DOM
    # snapshots that run to tens of kilobytes each.
    "max_log_message_length": (2000, 20_000),
    # Characters per tool result, after all of the above. The last line of
    # defence against one tool call filling the context window.
    "max_tool_result_chars": (24_000, 120_000),
    # How far back a query may look when the caller does not say.
    "default_lookback_hours": (4, 72),
    # How far back a query may look when the caller does ask. Logs Insights
    # bills by bytes scanned, so this is the single most expensive knob here.
    "max_lookback_hours": (72, 336),
    # How long to wait for one Logs Insights query to finish. Queries over a
    # wide time range on a busy group routinely take 30s+.
    "query_timeout_seconds": (45, 120),
    # Poll interval while a Logs Insights query runs.
    "query_poll_seconds": (1, 10),
}


def _read(name: str) -> int:
    """Resolve one limit from the environment, clamped to its ceiling.

    Every departure from what the operator asked for is logged. A limit that
    silently became something else is the kind of thing somebody debugs for an
    hour: the agent would stop earlier than the configured value says it
    should, and nothing anywhere would explain why.
    """
    variable = f"TROUBLESHOOTING_{name.upper()}"
    default, maximum = _LIMITS[name]
    raw = os.environ.get(variable, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        LOGGER.warning(
            "%s=%r is not a whole number; using the default of %d", variable, raw, default
        )
        return default
    if value < 1:
        LOGGER.warning(
            "%s=%d is not a usable limit; using the default of %d", variable, value, default
        )
        return default
    if value > maximum:
        LOGGER.warning(
            "%s=%d is above the maximum of %d for this setting; using %d",
            variable,
            value,
            maximum,
            maximum,
        )
        return maximum
    return value


@dataclass(frozen=True)
class AgentLimits:
    """Resolved limits for one agent run.

    Built once per invocation rather than at module import, so a change to the
    function's environment takes effect on the next invocation instead of on
    the next cold start.
    """

    max_iterations: int = field(default_factory=lambda: _read("max_iterations"))
    max_output_tokens: int = field(default_factory=lambda: _read("max_output_tokens"))
    token_budget: int = field(default_factory=lambda: _read("token_budget"))
    wall_clock_seconds: int = field(default_factory=lambda: _read("wall_clock_seconds"))
    answer_reserve_seconds: int = field(
        default_factory=lambda: _read("answer_reserve_seconds")
    )
    max_log_groups: int = field(default_factory=lambda: _read("max_log_groups"))
    max_log_events: int = field(default_factory=lambda: _read("max_log_events"))
    max_log_message_length: int = field(
        default_factory=lambda: _read("max_log_message_length")
    )
    max_tool_result_chars: int = field(
        default_factory=lambda: _read("max_tool_result_chars")
    )
    default_lookback_hours: int = field(
        default_factory=lambda: _read("default_lookback_hours")
    )
    max_lookback_hours: int = field(default_factory=lambda: _read("max_lookback_hours"))
    query_timeout_seconds: int = field(
        default_factory=lambda: _read("query_timeout_seconds")
    )
    query_poll_seconds: int = field(default_factory=lambda: _read("query_poll_seconds"))

    def resolve_lookback_hours(self, requested: Any) -> int:
        """Clamp a caller-supplied lookback to the configured window.

        The agent chooses this value, so it is model output and gets the same
        treatment as any other untrusted number: a missing, non-numeric or
        non-positive value falls back to the default, and anything above the
        ceiling is clamped rather than refused.
        """
        if requested is None:
            return self.default_lookback_hours
        try:
            hours = int(float(requested))
        except (TypeError, ValueError):
            return self.default_lookback_hours
        if hours < 1:
            return self.default_lookback_hours
        return min(hours, self.max_lookback_hours)

    def as_dict(self) -> Dict[str, int]:
        """The resolved limits, for logging at the start of a run."""
        return {f.name: getattr(self, f.name) for f in fields(self)}


def truncate(text: str, limit: int) -> str:
    """Cut `text` to `limit` characters, saying so where the cut happened.

    The marker matters: a model handed a silently truncated log extract will
    reason about the absence of a line that is simply past the cut, and state
    that absence as evidence. Telling it the extract was cut makes "I need a
    narrower query" available as a next step.
    """
    if limit < 1 or len(text) <= limit:
        return text
    kept = text[:limit]
    dropped = len(text) - limit
    return f"{kept}\n… [truncated: {dropped} more characters not shown]"
