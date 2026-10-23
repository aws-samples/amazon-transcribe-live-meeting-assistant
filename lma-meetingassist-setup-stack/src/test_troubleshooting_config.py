#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""Unit tests for the troubleshooting agent's limits (no AWS)."""

from __future__ import annotations

from unittest import mock

import pytest
from troubleshooting_config import _LIMITS, AgentLimits, truncate


def limits_with(**env: str) -> AgentLimits:
    """Build AgentLimits with these TROUBLESHOOTING_* variables set."""
    environ = {f"TROUBLESHOOTING_{k.upper()}": v for k, v in env.items()}
    with mock.patch.dict("os.environ", environ, clear=False):
        return AgentLimits()


def test_the_defaults_are_the_values_this_module_declares() -> None:
    """The dataclass must not drift from the table the ceilings live in."""
    limits = limits_with()

    for name, (default, _maximum) in _LIMITS.items():
        assert getattr(limits, name) == default, name


def test_every_declared_limit_has_a_field() -> None:
    """A limit in the table with no field would be silently unreachable."""
    limits = limits_with()

    assert set(limits.as_dict()) == set(_LIMITS)


def test_an_operator_can_lower_a_limit() -> None:
    assert limits_with(max_iterations="3").max_iterations == 3


def test_a_limit_above_its_ceiling_is_clamped_rather_than_refused() -> None:
    """A mistyped value should cost money, not stop a diagnosis from starting."""
    _default, maximum = _LIMITS["max_lookback_hours"]

    assert limits_with(max_lookback_hours="100000").max_lookback_hours == maximum


@pytest.mark.parametrize("value", ["", "   ", "not-a-number", "0", "-5", "1.5.2"])
def test_an_unusable_value_falls_back_to_the_default(value: str) -> None:
    default, _maximum = _LIMITS["max_log_events"]

    assert limits_with(max_log_events=value).max_log_events == default


def test_limits_are_read_per_run_not_per_cold_start() -> None:
    """A change to the function's environment applies to the next invocation.

    The fields use default_factory for exactly this; a module-level constant
    would hold the value from whenever the container started.
    """
    first = limits_with(max_iterations="4")
    second = limits_with(max_iterations="6")

    assert (first.max_iterations, second.max_iterations) == (4, 6)


def test_the_wall_clock_budget_stays_inside_the_lambda_timeout() -> None:
    """The ceiling has to leave the agent room to say it ran out of time.

    The function's timeout is 300s by default and the template allows raising
    it; 870 keeps the ceiling inside the 900s Lambda maximum either way.
    """
    _default, maximum = _LIMITS["wall_clock_seconds"]

    assert maximum < 900


# ── lookback resolution ─────────────────────────────────────────────────────
#
# This value comes from the model, so it gets the same treatment as any other
# untrusted number.


def test_an_absent_lookback_uses_the_default_window() -> None:
    limits = limits_with()

    assert limits.resolve_lookback_hours(None) == limits.default_lookback_hours


def test_a_requested_lookback_is_honoured_when_it_is_inside_the_window() -> None:
    assert limits_with().resolve_lookback_hours(12) == 12


def test_a_lookback_beyond_the_window_is_clamped() -> None:
    limits = limits_with(max_lookback_hours="48")

    assert limits.resolve_lookback_hours(500) == 48


@pytest.mark.parametrize("requested", ["", "lots", None, 0, -3, [], {}])
def test_an_unusable_lookback_falls_back_rather_than_raising(requested: object) -> None:
    limits = limits_with()

    assert limits.resolve_lookback_hours(requested) == limits.default_lookback_hours


def test_a_lookback_sent_as_a_string_is_parsed() -> None:
    """Tool arguments arrive as whatever the model serialised them to."""
    assert limits_with().resolve_lookback_hours("6") == 6


def test_a_fractional_lookback_is_truncated_not_rejected() -> None:
    assert limits_with().resolve_lookback_hours(2.9) == 2


# ── truncation ──────────────────────────────────────────────────────────────


def test_text_within_the_limit_is_returned_unchanged() -> None:
    assert truncate("short", 100) == "short"


def test_text_at_exactly_the_limit_is_not_truncated() -> None:
    assert truncate("abcde", 5) == "abcde"


def test_truncated_text_says_that_it_was_truncated() -> None:
    """A silent cut makes the model reason about a line that is past the cut.

    Reporting the cut leaves "ask a narrower question" available to it.
    """
    result = truncate("abcdefghij", 4)

    assert result.startswith("abcd")
    assert "truncated" in result
    assert "6 more characters" in result


def test_a_nonsensical_limit_returns_the_text_rather_than_nothing() -> None:
    assert truncate("abc", 0) == "abc"
