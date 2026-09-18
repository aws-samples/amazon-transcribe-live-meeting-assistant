#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""End-to-end tests for Kinesis partial batch reporting (no AWS calls).

The handler is driven over the *real* batch processor from the transcript
enrichment layer: only the AWS clients, the AppSync session and the per-record
mutation are replaced. That is the point of this file — record decoding, the
failure bookkeeping and the mapping between a failing mutation and its record all
come from the layer, so stubbing the layer would leave nothing under test.

It therefore needs the Lambda's runtime dependencies (Powertools, gql and the
layer's own imports) to be importable, and skips itself where they are not — the
lint/test environment the root Makefile's setup-python target builds installs the
lint tooling only. ``test_batch_item_failures.py`` covers the same module's
decision logic with no such requirement, so the contract stays verified
everywhere.
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
for path in (str(HERE), str(LAYER)):
    if path not in sys.path:
        sys.path.insert(0, path)

os.environ.setdefault("APPSYNC_GRAPHQL_URL", "https://example.invalid/graphql")
os.environ.setdefault("STATE_DYNAMODB_TABLE_NAME", "event-sourcing")
os.environ.setdefault("PARAMETER_STORE_NAME", "lma-settings")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")

_BOTO3_SESSION = mock.MagicMock()
_BOTO3_SESSION.client.return_value.get_parameter.return_value = {
    "Parameter": {"Value": json.dumps({})}
}

try:
    with mock.patch("boto3.Session", return_value=_BOTO3_SESSION):
        import lambda_function  # noqa: E402
except ModuleNotFoundError as exc:  # pragma: no cover - depends on the environment
    pytest.skip(
        f"needs the Lambda's runtime dependencies; {exc.name} is not installed. "
        "test_batch_item_failures.py covers the reporting contract without them.",
        allow_module_level=True,
    )


class _FakeAppsyncClient:
    """Stands in for AppsyncAioGqlClient; yields an unused session."""

    async def __aenter__(self):
        return mock.MagicMock()

    async def __aexit__(self, *_args):
        return False


class _RecordingSqsClient:
    """Captures send_message calls; optionally fails them."""

    def __init__(self, error: Exception | None = None) -> None:
        self.messages: List[Dict[str, Any]] = []
        self._error = error

    def send_message(self, QueueUrl: str, MessageBody: str) -> Dict:  # noqa: N803
        if self._error:
            raise self._error
        self.messages.append({"queue_url": QueueUrl, "body": json.loads(MessageBody)})
        return {"MessageId": "test-message-id"}


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


def test_an_undecodable_record_is_copied_to_the_discarded_records_queue() -> None:
    """The skip is durable: the shard, sequence number and raw payload are kept."""
    event = kinesis_event(
        ["100", "200"],
        payloads=[b"this is not json", {"EventType": "END", "CallId": "call-200"}],
    )
    sqs = _RecordingSqsClient()

    with (
        mock.patch.object(lambda_function, "SQS_CLIENT", sqs),
        mock.patch.object(
            lambda_function, "DISCARDED_RECORDS_QUEUE_URL", "https://sqs.invalid/queue"
        ),
    ):
        response = invoke(event)

    assert response == {"batchItemFailures": []}
    assert len(sqs.messages) == 1
    message = sqs.messages[0]
    assert message["queue_url"] == "https://sqs.invalid/queue"
    assert message["body"]["sequenceNumber"] == "100"
    assert message["body"]["shardId"] == "shardId-000000000000"
    assert base64.b64decode(message["body"]["data"]) == b"this is not json"


def test_a_discarded_records_queue_failure_does_not_fail_the_invocation() -> None:
    """The record was already skipped; failing would redeliver good records."""
    from botocore.exceptions import ClientError

    event = kinesis_event(["100", "200"], payloads=[b"not json", {"CallId": "call-200"}])
    sqs = _RecordingSqsClient(
        error=ClientError({"Error": {"Code": "AccessDenied", "Message": "no"}}, "SendMessage")
    )

    with (
        mock.patch.object(lambda_function, "SQS_CLIENT", sqs),
        mock.patch.object(
            lambda_function, "DISCARDED_RECORDS_QUEUE_URL", "https://sqs.invalid/queue"
        ),
    ):
        response = invoke(event)

    assert response == {"batchItemFailures": []}


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


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
