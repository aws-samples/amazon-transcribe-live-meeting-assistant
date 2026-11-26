# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""The run lease (no AWS), on a fake table that evaluates the module's conditions,
and which transcript segments a run reads next (lma_live_pass.segments)."""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict

import pytest
from botocore.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lma_live_pass import LivePass  # noqa: E402
from lma_live_pass import segments as live_pass  # noqa: E402


def _number(attr: Dict[str, str]) -> float:
    return float(attr["N"])


class FakeTable:
    """Holds one state row and applies the update expressions LivePass writes."""

    def __init__(self) -> None:
        self.row: Dict[str, Dict[str, str]] = {}

    def get_item(self, **_kwargs: Any) -> Dict[str, Any]:
        return {"Item": dict(self.row)} if self.row else {}

    def _fail(self) -> None:
        raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem")

    def _holds(self, condition: str, values: Dict[str, Dict[str, str]]) -> bool:
        row = self.row
        for clause in condition.split(" AND "):
            clause = clause.strip("() ")
            if clause.startswith("attribute_not_exists(LeaseUntil)"):
                if "LeaseUntil" in row and _number(row["LeaseUntil"]) >= _number(values[":now"]):
                    return False
            elif clause.startswith("attribute_not_exists(RunCount)"):
                if "RunCount" in row and _number(row["RunCount"]) >= _number(values[":max"]):
                    return False
            elif clause.startswith("attribute_not_exists(LastRunAt)"):
                if "LastRunAt" in row and _number(row["LastRunAt"]) > _number(values[":due"]):
                    return False
            elif clause == "LeaseUntil = :lease":
                if row.get("LeaseUntil") != values[":lease"]:
                    return False
            else:  # pragma: no cover - a condition the fake does not know
                raise AssertionError(clause)
        return True

    def update_item(self, **kwargs: Any) -> Dict[str, Any]:
        values = kwargs["ExpressionAttributeValues"]
        names = kwargs.get("ExpressionAttributeNames", {})
        if not self._holds(kwargs["ConditionExpression"], values):
            self._fail()
        expression = kwargs["UpdateExpression"]
        set_part = expression.split(" REMOVE ")[0]
        if set_part.startswith("SET "):
            for assignment in re.split(r", (?=[#\w]+ = )", set_part[4:]):
                name, value = [s.strip() for s in assignment.split(" = ", 1)]
                name = names.get(name, name)
                if value.startswith("if_not_exists(RunCount"):
                    current = _number(self.row["RunCount"]) if "RunCount" in self.row else 0
                    self.row[name] = {"N": str(int(current) + 1)}
                elif value.startswith("if_not_exists("):
                    placeholder = re.search(r", (:\w+)\)", value).group(1)
                    self.row.setdefault(name, values[placeholder])
                else:
                    self.row[name] = values[value]
        if "REMOVE LeaseUntil" in expression:
            self.row.pop("LeaseUntil", None)
        return {"Attributes": dict(self.row)}


@pytest.fixture
def lease() -> LivePass:
    return LivePass(FakeTable(), "events", prefix="wb#", sort_key="updater", lease_seconds=180)


def test_one_run_holds_the_meeting_at_a_time(lease: LivePass) -> None:
    first = lease.claim("c1", now=1000, interval_seconds=180, max_runs=60, expires=99999)
    assert first and first["LeaseUntil"] == 1180 and first["ExpiresAfter"] == 99999
    assert lease.claim("c1", now=1001, interval_seconds=180, max_runs=60) is None


def test_a_released_run_counts_and_waits_an_interval(lease: LivePass) -> None:
    state = lease.claim("c1", now=1000, interval_seconds=180, max_runs=60)
    lease.release(
        "c1", state["LeaseUntil"], ran=True, now=1000, Notes="so far", LastSegmentEndTime=42.5
    )
    row = lease.read("c1")
    assert row["RunCount"] == 1 and row["Notes"] == "so far" and row["LastSegmentEndTime"] == 42.5
    assert "LeaseUntil" not in row
    assert lease.claim("c1", now=1100, interval_seconds=180, max_runs=60) is None
    assert lease.claim("c1", now=1180, interval_seconds=180, max_runs=60) is not None


def test_a_run_that_ignores_the_interval_still_respects_the_lease(lease: LivePass) -> None:
    state = lease.claim("c1", now=1000, interval_seconds=180, max_runs=60)
    lease.release("c1", state["LeaseUntil"], ran=True, now=1000)
    assert lease.claim("c1", now=1010, interval_seconds=180, max_runs=60, ignore_interval=True)
    assert (
        lease.claim("c1", now=1011, interval_seconds=180, max_runs=60, ignore_interval=True) is None
    )


def test_no_run_starts_once_the_meeting_has_used_its_runs(lease: LivePass) -> None:
    for at in (1000, 2000):
        state = lease.claim("c1", now=at, interval_seconds=60, max_runs=2)
        lease.release("c1", state["LeaseUntil"], ran=True, now=at)
    assert lease.claim("c1", now=3000, interval_seconds=60, max_runs=2) is None
    assert (
        lease.claim("c1", now=3000, interval_seconds=60, max_runs=2, ignore_interval=True) is None
    )


def test_a_release_without_a_run_does_not_count(lease: LivePass) -> None:
    state = lease.claim("c1", now=1000, interval_seconds=180, max_runs=60)
    lease.release("c1", state["LeaseUntil"], ran=False, now=1000)
    assert "RunCount" not in lease.read("c1")
    assert lease.claim("c1", now=1001, interval_seconds=180, max_runs=60) is not None


def test_a_run_that_outlived_its_lease_cannot_clear_anothers(lease: LivePass) -> None:
    old = lease.claim("c1", now=1000, interval_seconds=60, max_runs=60)
    new = lease.claim("c1", now=1200, interval_seconds=60, max_runs=60)
    assert new is not None
    lease.release("c1", old["LeaseUntil"], ran=True, now=1300)
    assert lease.read("c1")["LeaseUntil"] == new["LeaseUntil"]


# ── which segments a run reads ───────────────────────────────────────────────


@dataclass
class _Segment:
    segment_id: str
    end: float


def _segment(segment_id: str, end: float) -> _Segment:
    return _Segment(segment_id, end)


def test_a_segment_finalized_after_the_last_run_but_ending_before_its_marker_is_new() -> None:
    """A long utterance on another channel can end before the marker and arrive after it."""
    segments = [_segment("a", 100), _segment("late", 95), _segment("b", 110), _segment("c", 130)]
    fresh = live_pass.new_segments(segments, 110, ["a", "b"])
    assert [s.segment_id for s in fresh] == ["late", "c"]


def test_a_segment_before_the_overlap_is_not_read_again() -> None:
    segments = [_segment("old", 110 - live_pass.OVERLAP_SECONDS - 1), _segment("c", 130)]
    assert [s.segment_id for s in live_pass.new_segments(segments, 110, [])] == ["c"]


def test_a_state_row_without_shown_ids_is_read_by_its_marker_alone() -> None:
    segments = [_segment("late", 95), _segment("c", 130)]
    assert [s.segment_id for s in live_pass.new_segments(segments, 110, None)] == ["c"]


def test_the_shown_ids_kept_are_those_near_the_marker() -> None:
    old = 110 - live_pass.OVERLAP_SECONDS - 1
    segments = [
        _segment("old", old),
        _segment("a", 100),
        _segment("b", 110),
        _segment("unseen", 120),
    ]
    assert live_pass.shown_near(segments, ["old", "a", "b"], 110) == ["a", "b"]


def test_after_a_row_without_shown_ids_what_its_marker_covered_is_not_read_again() -> None:
    """A meeting running during a stack update: its earlier runs read up to the marker."""
    segments = [_segment("a", 90), _segment("b", 110), _segment("c", 150)]
    fresh = live_pass.new_segments(segments, 110, None)
    assert [s.segment_id for s in fresh] == ["c"]
    recorded = live_pass.shown_after_run(segments, 110, None, fresh)
    assert recorded == ["a", "b", "c"]
    # The next run, after a late segment ending inside the overlap, reads only that one.
    later = [*segments, _segment("late", 100), _segment("d", 200)]
    assert [s.segment_id for s in live_pass.new_segments(later, 150, recorded)] == ["late", "d"]


def test_a_first_run_records_its_segments_near_its_marker() -> None:
    segments = [_segment("a", 10), _segment("b", 200)]
    assert live_pass.next_marker(segments, None) == 200
    assert live_pass.shown_after_run(segments, None, None, segments) == ["b"]


def test_the_marker_never_moves_back() -> None:
    assert live_pass.next_marker([_segment("late", 95)], 110) == 110
