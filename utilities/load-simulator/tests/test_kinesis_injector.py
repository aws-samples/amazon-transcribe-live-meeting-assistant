# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
"""Unit tests for the kinesis_injector driver."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from lma_load.drivers.kinesis_injector import (
    KinesisInjector,
    SyntheticMeetingSpec,
    _build_events,
    _phase_events,
    make_backfill_specs,
    make_synthetic_summary,
)


def test_build_events_shapes_match_lma_pipeline():
    spec = SyntheticMeetingSpec(
        call_id="lt-test-0001 - 2026-01-01T10:00:00",
        owner="loadtest-lt-test-u0001",
        created_at=datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc),
        duration_s=60.0,
        segment_count=3,
    )
    events = list(_build_events(spec))
    assert len(events) == 1 + 3 + 1  # START, 3 segs, END

    start, s1, s2, s3, end = events
    assert start["EventType"] == "START"
    assert start["CallId"] == spec.call_id
    assert start["AgentId"] == spec.owner
    assert "CreatedAt" in start

    assert s1["EventType"] == "ADD_TRANSCRIPT_SEGMENT"
    # StartTime must be strictly > 0.0 to work around the truthiness bug in
    # normalize_transcript_segments (falsy 0.0 → non-null Float GraphQL error).
    assert s1["StartTime"] > 0.0
    assert s1["StartTime"] < 1.0
    assert s3["EndTime"] <= 60.0
    assert s1["EndTime"] > s1["StartTime"]
    assert s1["Channel"] in ("AGENT", "CALLER")
    assert s1["IsPartial"] is False


    assert end["EventType"] == "END"
    assert end["CallId"] == spec.call_id
    assert end["UpdatedAt"] > end["CreatedAt"]


def test_build_events_appends_add_summary_when_spec_has_summary_text():
    spec = SyntheticMeetingSpec(
        call_id="lt-summary-0001",
        owner="loadtest-lt-summary-u0001",
        created_at=datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc),
        duration_s=60.0,
        segment_count=1,
        summary_text="# Synthetic Summary\n\nHello.",
    )
    events = list(_build_events(spec))
    # START + 1 segment + ADD_SUMMARY + END
    # (ADD_SUMMARY is emitted BEFORE END so its UpdatedAt is strictly smaller
    # than END's — see kinesis_injector._phase_events for the VTL ``<`` guard
    # explanation.)
    assert len(events) == 4
    types = [e["EventType"] for e in events]
    assert types == ["START", "ADD_TRANSCRIPT_SEGMENT", "ADD_SUMMARY", "END"]
    add_summary = next(e for e in events if e["EventType"] == "ADD_SUMMARY")
    end_event = next(e for e in events if e["EventType"] == "END")
    assert add_summary["CallId"] == spec.call_id
    assert add_summary["CallSummaryText"].startswith("# Synthetic Summary")
    # END's UpdatedAt must be strictly greater than ADD_SUMMARY's so the
    # VTL condition ``#UpdatedAt < :UpdatedAt`` passes in-batch regardless
    # of which mutation asyncio.gather picks up first.
    assert end_event["UpdatedAt"] > add_summary["UpdatedAt"]



def test_phase_events_are_disjoint_and_complete():
    """Union of the three phases == what _build_events yields."""
    spec = SyntheticMeetingSpec(
        call_id="lt-phase-0001",
        owner="u",
        created_at=datetime.now(timezone.utc),
        duration_s=60.0,
        segment_count=5,
        summary_text="canned",
    )
    combined = list(_build_events(spec))
    phased = (
        list(_phase_events("START", spec))
        + list(_phase_events("SEGMENTS", spec))
        + list(_phase_events("END", spec))
    )
    assert len(phased) == len(combined)
    assert [e["EventType"] for e in phased] == [e["EventType"] for e in combined]


def test_emit_phase_only_emits_that_phase():
    """emit_phase('START') must buffer exactly the START events (one per spec)."""
    mock_client = MagicMock()
    mock_client.put_records.return_value = {"FailedRecordCount": 0, "Records": []}
    injector = KinesisInjector(stream_name="t", region="us-east-1", client=mock_client)

    specs = [
        SyntheticMeetingSpec(
            call_id=f"lt-start-{i}",
            owner="u",
            created_at=datetime.now(timezone.utc),
            duration_s=30.0,
            segment_count=5,
        )
        for i in range(100)
    ]

    # Only STARTs — 100 meetings → 100 records.
    n = injector.emit_phase("START", specs)
    assert n == 100
    assert injector.stats["emitted"] == 100

    # Then segments — 100 × 5 = 500 records.
    n = injector.emit_phase("SEGMENTS", specs)
    assert n == 500
    assert injector.stats["emitted"] == 100 + 500

    # Then ENDs — 100 records.
    n = injector.emit_phase("END", specs)
    assert n == 100
    assert injector.stats["emitted"] == 100 + 500 + 100

    # And every Kinesis record's EventType matches its phase.
    all_records = []
    for call in mock_client.put_records.call_args_list:
        all_records.extend(call.kwargs["Records"])
    event_types = [json.loads(r["Data"])["EventType"] for r in all_records]
    assert event_types.count("START") == 100
    assert event_types.count("ADD_TRANSCRIPT_SEGMENT") == 500
    assert event_types.count("END") == 100


def test_emit_phase_rejects_unknown_phase():
    injector = KinesisInjector(stream_name="t", region="us-east-1", client=MagicMock())
    try:
        injector.emit_phase("WRONG", [])
    except ValueError as err:
        assert "Unknown phase" in str(err)
    else:
        raise AssertionError("emit_phase should reject unknown phase names")


def test_make_backfill_specs_spread_across_days():
    specs = make_backfill_specs(
        count=500,
        run_id="lt-unit-testXXX",
        days_back=30,
        owners=["u1", "u2", "u3"],
    )
    assert len(specs) == 500
    timestamps = [s.created_at for s in specs]
    assert timestamps == sorted(timestamps)
    now = datetime.now(timezone.utc)
    assert all(s.created_at >= now - timedelta(days=31) for s in specs)
    assert all(s.created_at <= now + timedelta(seconds=60) for s in specs)
    owners_used = {s.owner for s in specs}
    assert owners_used == {"u1", "u2", "u3"}
    # include_synthetic_summary defaults False → no summary_text.
    assert all(s.summary_text is None for s in specs)


def test_make_backfill_specs_with_synthetic_summary_populates_all():
    specs = make_backfill_specs(
        count=10,
        run_id="lt-syn",
        days_back=7,
        owners=["u"],
        include_synthetic_summary=True,
    )
    assert all(s.summary_text for s in specs)
    assert all("Synthetic Meeting Summary" in s.summary_text for s in specs)


def test_kinesis_injector_batches_and_flushes():
    mock_client = MagicMock()
    mock_client.put_records.return_value = {"FailedRecordCount": 0, "Records": []}

    injector = KinesisInjector(
        stream_name="test-stream",
        region="us-east-1",
        client=mock_client,
    )
    specs = [
        SyntheticMeetingSpec(
            call_id=f"lt-bulk-{i}",
            owner="owner",
            created_at=datetime.now(timezone.utc),
            duration_s=30.0,
            segment_count=1,
        )
        for i in range(200)  # 200 meetings × 3 events = 600 records
    ]
    injector.emit_many(specs)
    assert mock_client.put_records.call_count >= 2
    assert injector.stats["emitted"] == 600
    assert injector.stats["failed"] == 0


def test_kinesis_injector_requeues_failed_records():
    mock_client = MagicMock()
    mock_client.put_records.side_effect = [
        {
            "FailedRecordCount": 1,
            "Records": [{"ShardId": "x"}, {"ErrorCode": "ProvisionedThroughputExceededException"}],
        },
        {"FailedRecordCount": 0, "Records": []},
    ]
    injector = KinesisInjector(stream_name="t", region="us-east-1", client=mock_client)
    spec = SyntheticMeetingSpec(
        call_id="lt-0001",
        owner="u",
        created_at=datetime.now(timezone.utc),
        duration_s=30.0,
        segment_count=0,  # → only START + END = 2 events
    )
    injector.emit_meeting(spec)
    injector.flush()
    assert injector.stats["emitted"] >= 1
    assert mock_client.put_records.call_count >= 2


def test_event_json_is_parseable():
    spec = SyntheticMeetingSpec(
        call_id="lt-parse-0001",
        owner="u",
        created_at=datetime.now(timezone.utc),
        duration_s=30.0,
        segment_count=2,
    )
    for ev in _build_events(spec):
        parsed = json.loads(json.dumps(ev))
        assert parsed["CallId"] == "lt-parse-0001"


def test_make_synthetic_summary_contains_run_id_and_persona():
    spec = SyntheticMeetingSpec(
        call_id="lt-summ-0001",
        owner="u",
        created_at=datetime.now(timezone.utc),
        duration_s=15 * 60,
        segment_count=8,
    )
    text = make_synthetic_summary(spec, "lt-run-abc")
    assert "lt-run-abc" in text
    assert "15 min" in text or "16 min" in text
    assert "lma-load-simulator" in text
