#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""Unit tests for the stale meeting reaper.

The DynamoDB table and the Kinesis stream are replaced with in-memory fakes, so
the real handler logic runs end to end without AWS.
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

FUNCTION_DIR = Path(__file__).resolve().parent
if str(FUNCTION_DIR) not in sys.path:
    sys.path.insert(0, str(FUNCTION_DIR))

TABLE_NAME = "test-event-sourcing-table"
STREAM_NAME = "test-call-data-stream"
TIMEOUT_MINUTES = 240

os.environ["EVENT_SOURCING_TABLE"] = TABLE_NAME
os.environ["CALL_DATA_STREAM_NAME"] = STREAM_NAME
os.environ["MEETING_INACTIVITY_TIMEOUT_IN_MINUTES"] = str(TIMEOUT_MINUTES)
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

with patch("boto3.resource", MagicMock()), patch("boto3.client", MagicMock()):
    import lambda_function as reaper  # noqa: E402


NOW = datetime.now(timezone.utc)


def iso(minutes_ago: float) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).isoformat()


def list_row(call_id: str, minutes_ago: float = TIMEOUT_MINUTES + 60) -> dict:
    """A `cls#…` meeting-list row as the TypeDateIndex GSI projects it."""
    started = NOW - timedelta(minutes=minutes_ago)
    return {
        "PK": f"cls#{started.date().isoformat()}#s0",
        "SK": f"ts#{started.isoformat()}#id#{call_id}",
        "ItemType": "call",
        "CallId": call_id,
        "CreatedAt": started.isoformat(),
        "Owner": f"{call_id}@example.com",
    }


def call_row(call_id: str, status: str = "TRANSCRIBING", updated_minutes_ago: float = 600) -> dict:
    return {
        "PK": f"c#{call_id}",
        "SK": f"c#{call_id}",
        "CallId": call_id,
        "Status": status,
        "Owner": f"{call_id}@example.com",
        "CreatedAt": iso(updated_minutes_ago + 60),
        "UpdatedAt": iso(updated_minutes_ago),
    }


def upload_job_row(call_id: str, status: str) -> dict:
    return {"PK": f"uj#{call_id}", "SK": f"uj#{call_id}", "Status": status}


class FakeTable:
    """Returns the configured list rows for any TypeDateIndex query."""

    def __init__(self, pages, recorded_queries):
        self._pages = pages
        self._recorded_queries = recorded_queries

    def query(self, **kwargs):
        self._recorded_queries.append(kwargs)
        start_key = kwargs.get("ExclusiveStartKey")
        page_index = int(start_key["page"]) if start_key else 0
        items = self._pages[page_index] if page_index < len(self._pages) else []
        response = {"Items": list(items)}
        if page_index + 1 < len(self._pages):
            response["LastEvaluatedKey"] = {"page": page_index + 1}
        return response


class FakeClient:
    """BatchGetItem fake that can withhold keys as unprocessed.

    ``unprocessed_rounds`` is how many responses hold the batch's first key back
    in ``UnprocessedKeys`` before answering in full, mimicking a throttled table.
    """

    def __init__(self, items_by_pk, unprocessed_rounds=0):
        self._items_by_pk = items_by_pk
        self._unprocessed_rounds = unprocessed_rounds
        self.call_count = 0

    def batch_get_item(self, RequestItems):  # noqa: N803 — boto3 parameter name
        self.call_count += 1
        keys = RequestItems[TABLE_NAME]["Keys"]
        if self._unprocessed_rounds > 0 and keys:
            self._unprocessed_rounds -= 1
            withheld, answered = keys[:1], keys[1:]
            found = [self._items_by_pk[k["PK"]] for k in answered if k["PK"] in self._items_by_pk]
            return {
                "Responses": {TABLE_NAME: found},
                "UnprocessedKeys": {TABLE_NAME: {"Keys": withheld, "ConsistentRead": True}},
            }
        found = [self._items_by_pk[k["PK"]] for k in keys if k["PK"] in self._items_by_pk]
        return {"Responses": {TABLE_NAME: found}}


class FakeDynamoDbResource:
    def __init__(self, list_rows, items, unprocessed_rounds=0):
        self.recorded_queries = []
        # A flat list of rows is the single-page case; a list of lists is paged.
        pages = list_rows if list_rows and isinstance(list_rows[0], list) else [list_rows]
        self._table = FakeTable(pages, self.recorded_queries)
        self.meta = MagicMock()
        self.meta.client = FakeClient(
            {item["PK"]: item for item in items},
            unprocessed_rounds=unprocessed_rounds,
        )

    def Table(self, name):  # noqa: N802 — boto3 method name
        assert name == TABLE_NAME
        return self._table


class FakeKinesisClient:
    def __init__(self, fail_for_call_ids=()):
        self.records = []
        self._fail_for_call_ids = set(fail_for_call_ids)

    def put_record(self, StreamName, PartitionKey, Data):  # noqa: N803
        if PartitionKey in self._fail_for_call_ids:
            raise ClientError(
                {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "slow"}},
                "PutRecord",
            )
        self.records.append(
            {
                "stream_name": StreamName,
                "partition_key": PartitionKey,
                "payload": json.loads(Data.decode("utf-8")),
            }
        )


@pytest.fixture(name="run_reaper")
def run_reaper_fixture(monkeypatch):
    """Runs the handler against fake AWS clients and returns (summary, kinesis)."""

    def _run(list_rows, items, fail_for_call_ids=(), unprocessed_rounds=0):
        dynamodb = FakeDynamoDbResource(list_rows, items, unprocessed_rounds=unprocessed_rounds)
        kinesis = FakeKinesisClient(fail_for_call_ids)
        monkeypatch.setattr(reaper, "DYNAMODB_RESOURCE", dynamodb)
        monkeypatch.setattr(reaper, "KINESIS_CLIENT", kinesis)
        summary = reaper.handler({}, MagicMock())
        return summary, kinesis, dynamodb

    return _run


def test_meeting_with_no_recent_activity_is_ended(run_reaper):
    summary, kinesis, _ = run_reaper(
        [list_row("abandoned")],
        [call_row("abandoned", status="TRANSCRIBING", updated_minutes_ago=TIMEOUT_MINUTES + 30)],
    )

    assert summary["ended"] == 1
    assert len(kinesis.records) == 1
    record = kinesis.records[0]
    assert record["stream_name"] == STREAM_NAME
    assert record["partition_key"] == "abandoned"
    assert record["payload"]["EventType"] == "END"
    assert record["payload"]["CallId"] == "abandoned"
    assert record["payload"]["AgentId"] == "abandoned@example.com"


def test_end_event_omits_updated_at_so_the_resolver_supplies_it(run_reaper):
    """updateCall.request.vtl only applies a mutation newer than the row, and
    falls back to resolver-time now when UpdatedAt is absent."""
    _, kinesis, _ = run_reaper(
        [list_row("abandoned")],
        [call_row("abandoned", updated_minutes_ago=TIMEOUT_MINUTES + 30)],
    )

    assert "UpdatedAt" not in kinesis.records[0]["payload"]


def test_meeting_with_activity_inside_the_timeout_is_left_open(run_reaper):
    summary, kinesis, _ = run_reaper(
        [list_row("live")],
        [call_row("live", status="TRANSCRIBING", updated_minutes_ago=TIMEOUT_MINUTES - 30)],
    )

    assert summary["ended"] == 0
    assert kinesis.records == []


def test_already_ended_meeting_is_left_alone(run_reaper):
    summary, kinesis, _ = run_reaper(
        [list_row("finished")],
        [call_row("finished", status="ENDED", updated_minutes_ago=TIMEOUT_MINUTES + 600)],
    )

    assert summary["ended"] == 0
    assert kinesis.records == []


def test_meeting_still_in_the_upload_pipeline_is_left_open(run_reaper):
    summary, kinesis, _ = run_reaper(
        [list_row("uploaded")],
        [
            call_row("uploaded", status="STARTED", updated_minutes_ago=TIMEOUT_MINUTES + 600),
            upload_job_row("uploaded", "TRANSCRIBING"),
        ],
    )

    assert summary["ended"] == 0
    assert kinesis.records == []


def test_meeting_whose_upload_job_finished_is_still_eligible(run_reaper):
    summary, kinesis, _ = run_reaper(
        [list_row("uploaded")],
        [
            call_row("uploaded", status="STARTED", updated_minutes_ago=TIMEOUT_MINUTES + 600),
            upload_job_row("uploaded", "FAILED"),
        ],
    )

    assert summary["ended"] == 1
    assert kinesis.records[0]["payload"]["CallId"] == "uploaded"


def test_meeting_without_updated_at_uses_created_at(run_reaper):
    row = call_row("legacy", status="STARTED", updated_minutes_ago=TIMEOUT_MINUTES + 30)
    del row["UpdatedAt"]
    summary, kinesis, _ = run_reaper([list_row("legacy")], [row])

    assert summary["ended"] == 1
    assert kinesis.records[0]["payload"]["CallId"] == "legacy"


def test_only_stale_meetings_in_a_mixed_batch_are_ended(run_reaper):
    summary, kinesis, _ = run_reaper(
        [list_row("stale-one"), list_row("live-one"), list_row("stale-two")],
        [
            call_row("stale-one", updated_minutes_ago=TIMEOUT_MINUTES + 10),
            call_row("live-one", updated_minutes_ago=1),
            call_row("stale-two", status="STARTED", updated_minutes_ago=TIMEOUT_MINUTES + 5000),
        ],
    )

    assert summary["ended"] == 2
    assert {r["payload"]["CallId"] for r in kinesis.records} == {"stale-one", "stale-two"}


def test_a_failed_put_record_does_not_stop_the_remaining_meetings(run_reaper):
    summary, kinesis, _ = run_reaper(
        [list_row("fails"), list_row("succeeds")],
        [
            call_row("fails", updated_minutes_ago=TIMEOUT_MINUTES + 10),
            call_row("succeeds", updated_minutes_ago=TIMEOUT_MINUTES + 10),
        ],
        fail_for_call_ids=["fails"],
    )

    assert summary["ended"] == 1
    assert summary["failed"] == 1
    assert [r["payload"]["CallId"] for r in kinesis.records] == ["succeeds"]


def test_missing_meeting_row_is_skipped(run_reaper):
    """A list row can outlive its detail row once the detail row's TTL fires."""
    summary, kinesis, _ = run_reaper([list_row("expired")], [])

    assert summary["candidates"] == 1
    assert summary["ended"] == 0
    assert kinesis.records == []


def test_run_is_capped_at_the_per_invocation_maximum(run_reaper, monkeypatch):
    monkeypatch.setattr(reaper, "MAX_MEETINGS_PER_RUN", 2)
    call_ids = [f"stale-{i}" for i in range(5)]
    summary, kinesis, _ = run_reaper(
        [list_row(cid) for cid in call_ids],
        [call_row(cid, updated_minutes_ago=TIMEOUT_MINUTES + 10) for cid in call_ids],
    )

    assert summary["stale"] == 5
    assert summary["ended"] == 2
    assert len(kinesis.records) == 2


def test_candidates_are_collected_across_query_pages(run_reaper):
    """A candidate on the second page is examined like one on the first."""
    pages = [[list_row("page-one-stale")], [list_row("page-two-stale")]]
    summary, kinesis, dynamodb = run_reaper(
        pages,
        [
            call_row("page-one-stale", updated_minutes_ago=TIMEOUT_MINUTES + 10),
            call_row("page-two-stale", updated_minutes_ago=TIMEOUT_MINUTES + 10),
        ],
    )

    assert len(dynamodb.recorded_queries) == 2
    assert dynamodb.recorded_queries[1]["ExclusiveStartKey"] == {"page": 1}
    assert summary["candidates"] == 2
    assert {r["payload"]["CallId"] for r in kinesis.records} == {
        "page-one-stale",
        "page-two-stale",
    }


def test_candidate_reads_stop_at_the_candidate_cap(run_reaper, monkeypatch):
    """Reads are bounded even when the window holds more meetings than the cap."""
    monkeypatch.setattr(reaper, "MAX_CANDIDATES", 1)
    pages = [[list_row("newest")], [list_row("older")]]
    summary, kinesis, dynamodb = run_reaper(
        pages,
        [
            call_row("newest", updated_minutes_ago=TIMEOUT_MINUTES + 10),
            call_row("older", updated_minutes_ago=TIMEOUT_MINUTES + 10),
        ],
    )

    assert len(dynamodb.recorded_queries) == 1
    assert summary["candidates"] == 1
    assert [r["payload"]["CallId"] for r in kinesis.records] == ["newest"]


def test_the_newest_candidates_are_read_first(run_reaper):
    """A meeting crosses the cutoff at the newest end of the window."""
    _, _, dynamodb = run_reaper(
        [list_row("abandoned")],
        [call_row("abandoned", updated_minutes_ago=TIMEOUT_MINUTES + 10)],
    )

    assert dynamodb.recorded_queries[0]["ScanIndexForward"] is False
    assert dynamodb.recorded_queries[0]["Limit"] > 0


def test_a_key_returned_as_unprocessed_is_requested_again(run_reaper):
    summary, kinesis, dynamodb = run_reaper(
        [list_row("throttled")],
        [call_row("throttled", updated_minutes_ago=TIMEOUT_MINUTES + 10)],
        unprocessed_rounds=1,
    )

    assert dynamodb.meta.client.call_count == 2
    assert summary["ended"] == 1
    assert kinesis.records[0]["payload"]["CallId"] == "throttled"


def test_keys_left_unprocessed_are_logged_rather_than_silently_dropped(run_reaper):
    """Sustained throttling must not make a run look like it found nothing."""
    with patch.object(reaper.LOGGER, "warning") as warning:
        summary, kinesis, dynamodb = run_reaper(
            [list_row("throttled")],
            [call_row("throttled", updated_minutes_ago=TIMEOUT_MINUTES + 10)],
            unprocessed_rounds=99,
        )

    assert dynamodb.meta.client.call_count == reaper.MAX_BATCH_GET_ATTEMPTS
    assert summary["ended"] == 0
    assert kinesis.records == []
    assert warning.call_count == 1


def test_candidate_query_upper_bound_is_the_inactivity_cutoff(run_reaper):
    _, _, dynamodb = run_reaper(
        [list_row("abandoned")],
        [call_row("abandoned", updated_minutes_ago=TIMEOUT_MINUTES + 10)],
    )

    assert len(dynamodb.recorded_queries) == 1
    query = dynamodb.recorded_queries[0]
    assert query["IndexName"] == "TypeDateIndex"
    item_type_eq, sort_key_between = query["KeyConditionExpression"].get_expression()["values"]
    assert item_type_eq.get_expression()["values"][1] == "call"
    _, lower_bound, upper_bound = sort_key_between.get_expression()["values"]
    lower = datetime.fromisoformat(lower_bound[len("ts#") :])
    upper = datetime.fromisoformat(upper_bound[len("ts#") : -len("#~")])
    assert upper <= datetime.now(timezone.utc) - timedelta(minutes=TIMEOUT_MINUTES)
    assert lower < upper


def test_timeout_of_zero_disables_the_reaper(run_reaper, monkeypatch):
    monkeypatch.setattr(reaper, "INACTIVITY_TIMEOUT_MINUTES", 0)
    summary, kinesis, dynamodb = run_reaper(
        [list_row("abandoned")],
        [call_row("abandoned", updated_minutes_ago=TIMEOUT_MINUTES + 10)],
    )

    assert summary == {"enabled": False, "candidates": 0, "ended": 0}
    assert kinesis.records == []
    assert dynamodb.recorded_queries == []


def test_call_id_is_recovered_from_a_list_row_without_the_call_id_attribute():
    row = list_row("legacy-row")
    del row["CallId"]

    assert reaper._call_id_from_list_row(row) == "legacy-row"


def test_zulu_and_offset_timestamps_both_parse():
    assert reaper._parse_iso("2026-01-02T03:04:05.000Z") == reaper._parse_iso(
        "2026-01-02T03:04:05+00:00"
    )
    assert reaper._parse_iso("not-a-timestamp") is None
    assert reaper._parse_iso(None) is None
