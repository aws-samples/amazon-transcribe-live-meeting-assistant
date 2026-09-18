#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""Unit tests for Kinesis partial batch failure reporting (no AWS calls).

The handler is driven end to end over the real batch processor: only the AWS
clients, the AppSync session and the per-record mutation are replaced.
"""

from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List
from unittest import mock

import pytest

HERE = Path(__file__).parent
# The function imports appsync_utils / transcript_batch_processor from the
# transcript enrichment Lambda layer, which is on the path at runtime only.
LAYER = HERE.parents[1] / "lambda_layers" / "transcript_enrichment_layer"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(LAYER))

os.environ.setdefault("APPSYNC_GRAPHQL_URL", "https://example.invalid/graphql")
os.environ.setdefault("STATE_DYNAMODB_TABLE_NAME", "event-sourcing")
os.environ.setdefault("PARAMETER_STORE_NAME", "lma-settings")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")

_BOTO3_SESSION = mock.MagicMock()
_BOTO3_SESSION.client.return_value.get_parameter.return_value = {
    "Parameter": {"Value": json.dumps({})}
}

with mock.patch("boto3.Session", return_value=_BOTO3_SESSION):
    import batch_item_failures  # noqa: E402
    import lambda_function  # noqa: E402


class _FakeAppsyncClient:
    """Stands in for AppsyncAioGqlClient; yields an unused session."""

    async def __aenter__(self):
        return mock.MagicMock()

    async def __aexit__(self, *_args):
        return False


def kinesis_event(sequence_numbers: List[str], payloads: List[Any] | None = None) -> Dict:
    """Builds a Kinesis batch, one record per sequence number."""
    if payloads is None:
        payloads = [{"EventType": "END", "CallId": f"call-{number}"} for number in sequence_numbers]
    records = []
    for sequence_number, payload in zip(sequence_numbers, payloads):
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        records.append(
            {
                "eventSource": "aws:kinesis",
                "eventID": f"shardId-000000000000:{sequence_number}",
                "kinesis": {
                    "kinesisSchemaVersion": "1.0",
                    "partitionKey": "pk",
                    "sequenceNumber": sequence_number,
                    "data": base64.b64encode(raw).decode("ascii"),
                    "approximateArrivalTimestamp": 1700000000.0,
                },
            }
        )
    return {"Records": records}


def invoke(event: Dict, failing_call_ids: set[str] | None = None) -> Dict:
    """Runs the handler, failing the mutation for the given CallIds."""
    failing = failing_call_ids or set()

    async def fake_mutation(*, message, **_kwargs):
        if message.get("CallId") in failing:
            raise RuntimeError(f"mutation failed for {message.get('CallId')}")
        return {"ok": True}

    context = mock.MagicMock()
    context.function_name = "CallEventProcessor"
    context.memory_limit_in_mb = 3000
    context.invoked_function_arn = (
        "arn:aws:lambda:us-east-1:123456789012:function:CallEventProcessor"
    )
    context.aws_request_id = "test-request-id"

    with (
        mock.patch.object(lambda_function, "APPSYNC_CLIENT", _FakeAppsyncClient()),
        mock.patch.object(lambda_function, "execute_process_event_api_mutation", fake_mutation),
    ):
        return lambda_function.handler(event, context)


def test_a_fully_successful_batch_reports_no_failures() -> None:
    response = invoke(kinesis_event(["100", "200", "300"]))

    assert response == {"batchItemFailures": []}


def test_a_single_failure_reports_that_record_sequence_number() -> None:
    event = kinesis_event(["100", "200", "300"])

    response = invoke(event, failing_call_ids={"call-200"})

    assert response == {"batchItemFailures": [{"itemIdentifier": "200"}]}


def test_an_all_failing_batch_reports_the_lowest_sequence_number() -> None:
    event = kinesis_event(["100", "200", "300"])

    response = invoke(event, failing_call_ids={"call-100", "call-200", "call-300"})

    assert response == {"batchItemFailures": [{"itemIdentifier": "100"}]}


def test_several_failures_report_only_the_earliest_of_them() -> None:
    """Kinesis resumes from the lowest reported number, so one entry suffices."""
    event = kinesis_event(["100", "200", "300", "400"])

    response = invoke(event, failing_call_ids={"call-200", "call-400"})

    assert response == {"batchItemFailures": [{"itemIdentifier": "200"}]}


def test_an_undecodable_record_is_not_reported_as_a_failure() -> None:
    """A record that will never decode must not hold up the rest of the shard."""
    event = kinesis_event(
        ["100", "200", "300"],
        payloads=[
            {"EventType": "END", "CallId": "call-100"},
            b"this is not json",
            {"EventType": "END", "CallId": "call-300"},
        ],
    )

    response = invoke(event)

    assert response == {"batchItemFailures": []}


def test_an_undecodable_record_does_not_mask_a_later_transient_failure() -> None:
    event = kinesis_event(
        ["100", "200", "300"],
        payloads=[
            b"this is not json",
            {"EventType": "END", "CallId": "call-200"},
            {"EventType": "END", "CallId": "call-300"},
        ],
    )

    response = invoke(event, failing_call_ids={"call-300"})

    assert response == {"batchItemFailures": [{"itemIdentifier": "300"}]}


def test_a_batch_level_failure_reports_the_first_record_in_the_batch() -> None:
    """An error that belongs to no single record retries the whole batch."""
    event = kinesis_event(["100", "200", "300"])

    class _FailingAppsyncClient:
        async def __aenter__(self):
            raise RuntimeError("cannot reach AppSync")

        async def __aexit__(self, *_args):
            return False

    context = mock.MagicMock()
    with mock.patch.object(lambda_function, "APPSYNC_CLIENT", _FailingAppsyncClient()):
        response = lambda_function.handler(event, context)

    assert response == {"batchItemFailures": [{"itemIdentifier": "100"}]}


def test_an_empty_batch_reports_no_failures() -> None:
    assert invoke({"Records": []}) == {"batchItemFailures": []}


def test_sequence_numbers_are_ordered_numerically_not_lexicographically() -> None:
    """ "9" is lower than "10"; a string sort would disagree."""
    assert batch_item_failures.select_checkpoint([], ["10", "9"]) == "9"


def test_delivery_order_wins_over_numeric_order() -> None:
    """The shard's own ordering is authoritative for what to resume from."""
    assert batch_item_failures.select_checkpoint(["30", "10", "20"], ["20", "10"]) == "10"


def test_no_failures_means_no_checkpoint() -> None:
    assert batch_item_failures.select_checkpoint(["10", "20"], []) is None


def test_sequence_numbers_are_read_from_either_record_shape() -> None:
    raw_record = {"kinesis": {"sequenceNumber": "42"}}
    assert batch_item_failures.record_sequence_number(raw_record) == "42"
    assert batch_item_failures.record_sequence_number({}) == ""
    assert batch_item_failures.sequence_numbers_in_batch(kinesis_event(["7", "8"])) == ["7", "8"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
