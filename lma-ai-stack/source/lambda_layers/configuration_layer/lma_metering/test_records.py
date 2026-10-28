#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Tests for writing metering records.

The properties that matter are the silent ones: a record lands under the
meeting's metering partition, carries only billable numbers, expires with the
meeting, and a failure to write it never reaches the feature that incurred the
usage.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List

import pytest

from lma_metering import records


class FakeDynamo:
    def __init__(self, fail: bool = False) -> None:
        self.items: List[Dict[str, Any]] = []
        self.fail = fail

    def put_item(self, TableName: str, Item: Dict[str, Any]) -> None:  # noqa: N803
        if self.fail:
            raise RuntimeError("throttled")
        self.items.append({"TableName": TableName, "Item": Item})


NOW = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)


def test_a_record_is_stored_under_the_meetings_metering_partition() -> None:
    item = records.build_item(
        "Meeting-1", "Summary", "bedrock/m", {"inputTokens": 10}, source="t", now=NOW
    )
    assert item["PK"] == {"S": "mtr#Meeting-1"}
    assert item["SK"]["S"].startswith("Summary#2026-10-28T12:00:00.000Z#")
    assert item["Usage"] == {"M": {"inputTokens": {"N": "10"}}}
    assert item["RecordedAt"] == {"S": "2026-10-28T12:00:00.000Z"}


def test_two_records_of_one_meeting_never_share_a_sort_key() -> None:
    first = records.build_item("c", "Summary", "s", {"requests": 1}, source="t", now=NOW)
    second = records.build_item("c", "Summary", "s", {"requests": 1}, source="t", now=NOW)
    assert first["SK"] != second["SK"]


def test_a_record_expires_with_the_meeting() -> None:
    item = records.build_item(
        "c", "Summary", "s", {"requests": 1}, source="t", expiration_days=0.5, now=NOW
    )
    assert int(item["ExpiresAfter"]["N"]) == int(NOW.timestamp()) + 43200


def test_the_retention_is_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(records.EXPIRATION_ENV_VAR, "90")
    item = records.build_item("c", "Summary", "s", {"requests": 1}, source="t", now=NOW)
    assert int(item["ExpiresAfter"]["N"]) == int(NOW.timestamp()) + 90 * 86400


def test_usage_keeps_only_finite_non_negative_numbers() -> None:
    item = records.build_item(
        "c",
        "Summary",
        "s",
        {"a": 1.25, "b": -1, "c": float("nan"), "d": "7", "e": True, "f": 0},
        source="t",
        now=NOW,
    )
    assert set(item["Usage"]["M"]) == {"a", "f"}


def test_a_record_with_no_usage_is_refused() -> None:
    with pytest.raises(ValueError):
        records.build_item("c", "Summary", "s", {"a": "x"}, source="t", now=NOW)


def test_bedrock_usage_takes_the_billed_units_and_not_the_total() -> None:
    usage = {
        "inputTokens": 1200,
        "outputTokens": 300,
        "totalTokens": 1500,
        "cacheReadInputTokens": 0,
        "cacheDetails": [{"ttl": "5m"}],
    }
    assert records.bedrock_usage(usage) == {
        "inputTokens": 1200,
        "outputTokens": 300,
        "requests": 1,
    }


def test_bedrock_usage_accepts_the_snake_case_shape() -> None:
    assert records.bedrock_usage({"input_tokens": 5, "output_tokens": 2}, requests=3) == {
        "inputTokens": 5,
        "outputTokens": 2,
        "requests": 3,
    }


def test_the_writer_stores_the_record() -> None:
    dynamo = FakeDynamo()
    writer = records.MeteringWriter("calls", source="summary", client=dynamo)
    assert writer.record("c", "Summary", "bedrock/m", {"inputTokens": 3}, details={"x": 1})
    stored = dynamo.items[0]
    assert stored["TableName"] == "calls"
    assert stored["Item"]["Source"] == {"S": "summary"}
    assert stored["Item"]["Details"] == {"M": {"x": {"S": "1"}}}


def test_a_failed_write_is_swallowed_and_reported_as_not_stored() -> None:
    writer = records.MeteringWriter("calls", source="summary", client=FakeDynamo(fail=True))
    assert writer.record("c", "Summary", "bedrock/m", {"inputTokens": 3}) is False


def test_a_writer_with_no_table_is_inert(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(records.TABLE_ENV_VAR, raising=False)
    dynamo = FakeDynamo()
    writer = records.MeteringWriter(source="summary", client=dynamo)
    assert writer.enabled is False
    assert writer.record("c", "Summary", "bedrock/m", {"inputTokens": 3}) is False
    assert dynamo.items == []


class FakeCounterTable:
    def __init__(self) -> None:
        self.requests: List[Dict[str, Any]] = []

    def update_item(self, **request: Any) -> None:
        self.requests.append(request)


def test_a_counter_adds_each_unit_to_one_item_per_meeting_and_service() -> None:
    table = FakeCounterTable()
    writer = records.MeteringWriter("calls", source="cep", client=table)
    assert writer.add("c", "Sentiment", "comprehend/detect-sentiment", {"units": 3, "requests": 1})
    request = table.requests[0]
    assert request["Key"] == {
        "PK": {"S": "mtr#c"},
        "SK": {"S": "Sentiment#counter#comprehend/detect-sentiment"},
    }
    assert " ADD " in request["UpdateExpression"]
    added = {
        request["ExpressionAttributeNames"][name]
        for name in request["ExpressionAttributeNames"]
        if name.startswith("#u")
    }
    assert added == {"u_units", "u_requests"}


def test_counter_attributes_are_read_back_as_usage() -> None:
    record = {"Usage": {"seconds": 2.0}, "u_units": 12, "u_requests": 4, "CallId": "c"}
    assert records.counter_usage(record) == {"seconds": 2.0, "units": 12.0, "requests": 4.0}


@pytest.mark.parametrize("length,units", [(0, 3), (99, 3), (301, 4), (1000, 10)])
def test_comprehend_bills_whole_hundreds_with_a_three_unit_minimum(length, units) -> None:
    assert records.comprehend_units("x" * length) == units


def test_a_record_without_a_meeting_is_not_written() -> None:
    dynamo = FakeDynamo()
    writer = records.MeteringWriter("calls", source="summary", client=dynamo)
    assert writer.record("", "Summary", "bedrock/m", {"inputTokens": 3}) is False
    assert dynamo.items == []
