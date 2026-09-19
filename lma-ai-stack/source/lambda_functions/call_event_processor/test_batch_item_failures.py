#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""Unit tests for the Kinesis partial batch failure contract (no AWS calls).

These cover the decisions the module makes on its own: which sequence number a
batch resumes from, and what a discarded record is recorded as. They import
``batch_item_failures`` with Powertools and the transcript enrichment layer
stubbed out, so they run in any environment — including one that has only the
lint/test tooling the root Makefile's setup-python target installs.

``test_partial_batch_processing.py`` covers the same module driven through the
real batch processor, and needs the Lambda's runtime dependencies to be present.
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path
from typing import Any, Dict, List
from unittest import mock

import pytest
from botocore.exceptions import ClientError

HERE = Path(__file__).parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))


class StubLogger:
    """Stands in for aws_lambda_powertools.Logger."""

    def __init__(self, *args, **kwargs):
        pass

    def debug(self, *args, **kwargs):
        pass

    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass

    def exception(self, *args, **kwargs):
        pass


class StubTranscriptBatchProcessor:
    """Stands in for the layer's batch processor, only so the subclass imports.

    Nothing here is exercised: the tests in this file call module-level functions.
    The subclass's behaviour is covered against the real base class in
    ``test_partial_batch_processing.py``.
    """


# Each dotted name needs its own entry: `from a.b.c import d` imports a, then
# a.b, then a.b.c, and a stub for `a` alone does not satisfy the later steps.
#
# The stubs are scoped to this import rather than left in sys.modules, so that
# test_partial_batch_processing.py imports the real Powertools and the real layer
# in the same pytest session — leaving them installed would silently hand the
# stubs to whichever module imported second.
_STUBS = {
    "aws_lambda_powertools": mock.MagicMock(Logger=StubLogger),
    "aws_lambda_powertools.utilities": mock.MagicMock(),
    "aws_lambda_powertools.utilities.typing": mock.MagicMock(),
    "transcript_batch_processor": mock.MagicMock(
        TranscriptBatchProcessor=StubTranscriptBatchProcessor
    ),
}

with mock.patch.dict(sys.modules, _STUBS):
    import batch_item_failures  # noqa: E402


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
                "eventSourceARN": "arn:aws:kinesis:us-east-1:123456789012:stream/CallDataStream",
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


class RecordingSqsClient:
    """Captures send_message calls; optionally fails them."""

    def __init__(self, error: Exception | None = None) -> None:
        self.messages: List[Dict[str, Any]] = []
        self._error = error

    def send_message(self, QueueUrl: str, MessageBody: str) -> Dict:  # noqa: N803
        if self._error:
            raise self._error
        self.messages.append({"queue_url": QueueUrl, "body": json.loads(MessageBody)})
        return {"MessageId": "test-message-id"}


# ---------------------------------------------------------------------------
# Which record the shard resumes from
# ---------------------------------------------------------------------------


def test_no_failures_means_no_checkpoint() -> None:
    assert batch_item_failures.select_checkpoint(["10", "20"], []) is None
    assert batch_item_failures.build_response(["10", "20"], []) == {"batchItemFailures": []}


def test_a_single_failure_selects_that_record() -> None:
    delivered = ["100", "200", "300"]

    assert batch_item_failures.select_checkpoint(delivered, ["200"]) == "200"
    assert batch_item_failures.build_response(delivered, ["200"]) == {
        "batchItemFailures": [{"itemIdentifier": "200"}]
    }


def test_an_all_failing_batch_selects_the_lowest_record() -> None:
    delivered = ["100", "200", "300"]

    assert batch_item_failures.select_checkpoint(delivered, delivered) == "100"


def test_several_failures_select_only_the_earliest_of_them() -> None:
    """Kinesis resumes from the lowest reported number, so one entry suffices."""
    delivered = ["100", "200", "300", "400"]

    assert batch_item_failures.select_checkpoint(delivered, ["400", "200"]) == "200"


def test_a_last_record_failure_selects_that_record() -> None:
    delivered = ["100", "200", "300"]

    assert batch_item_failures.select_checkpoint(delivered, ["300"]) == "300"


def test_sequence_numbers_are_ordered_numerically_not_lexicographically() -> None:
    """ "9" is lower than "10"; a string sort would disagree."""
    assert batch_item_failures.select_checkpoint([], ["10", "9"]) == "9"


def test_delivery_order_wins_over_numeric_order() -> None:
    """The shard's own ordering is authoritative for what to resume from.

    Delivery order picks "20" here while a numeric comparison would pick "10",
    so the two rules disagree and the assertion discriminates between them.
    """
    assert batch_item_failures.select_checkpoint(["30", "20", "10"], ["20", "10"]) == "20"


def test_an_empty_sequence_number_is_never_reported() -> None:
    """It would be an identifier the event source mapping cannot act on."""
    assert batch_item_failures.select_checkpoint(["100"], [""]) is None
    assert batch_item_failures.build_response(["100"], ["", None]) == {"batchItemFailures": []}


def test_sequence_numbers_are_read_from_either_record_shape() -> None:
    raw_record = {"kinesis": {"sequenceNumber": "42"}}
    assert batch_item_failures.record_sequence_number(raw_record) == "42"
    assert batch_item_failures.record_sequence_number({}) == ""
    assert batch_item_failures.sequence_numbers_in_batch(kinesis_event(["7", "8"])) == ["7", "8"]
    assert batch_item_failures.sequence_numbers_in_batch({"Records": []}) == []


# ---------------------------------------------------------------------------
# What a discarded record is recorded as
# ---------------------------------------------------------------------------


def test_a_discarded_record_carries_the_coordinates_needed_to_find_it_again() -> None:
    event = kinesis_event(["100", "200"], payloads=[b"not json", {"CallId": "call-200"}])

    messages = batch_item_failures.discarded_record_messages(event, ["100"])

    assert len(messages) == 1
    message = messages[0]
    assert message["sequenceNumber"] == "100"
    assert message["shardId"] == "shardId-000000000000"
    assert message["eventID"] == "shardId-000000000000:100"
    assert message["partitionKey"] == "pk"
    assert message["eventSourceARN"].endswith("stream/CallDataStream")
    assert base64.b64decode(message["data"]) == b"not json"


def test_only_the_named_records_are_described() -> None:
    event = kinesis_event(["100", "200", "300"])

    messages = batch_item_failures.discarded_record_messages(event, ["300"])

    assert [m["sequenceNumber"] for m in messages] == ["300"]
    assert batch_item_failures.discarded_record_messages(event, []) == []


def test_an_oversized_payload_is_truncated_so_the_message_still_lands() -> None:
    """An SQS message has a size limit; the metadata matters more than the tail."""
    oversized = b"x" * (batch_item_failures.MAX_DATA_CHARS * 2)
    event = kinesis_event(["100"], payloads=[oversized])

    messages = batch_item_failures.discarded_record_messages(event, ["100"])

    assert len(messages[0]["data"]) == batch_item_failures.MAX_DATA_CHARS
    assert messages[0]["dataTruncated"] == "true"
    assert int(messages[0]["dataLength"]) > batch_item_failures.MAX_DATA_CHARS
    assert messages[0]["sequenceNumber"] == "100"


def test_a_payload_within_the_limit_is_not_marked_truncated() -> None:
    messages = batch_item_failures.discarded_record_messages(
        kinesis_event(["100"], payloads=[b"small"]), ["100"]
    )

    assert base64.b64decode(messages[0]["data"]) == b"small"
    assert "dataTruncated" not in messages[0]


def test_discarded_records_are_sent_to_the_configured_queue() -> None:
    sqs = RecordingSqsClient()
    event = kinesis_event(["100", "200"], payloads=[b"not json", b"also not json"])

    sent = batch_item_failures.send_discarded_records(
        sqs_client=sqs,
        queue_url="https://sqs.invalid/queue",
        event=event,
        sequence_numbers=["100", "200"],
    )

    assert sent == 2
    assert [m["body"]["sequenceNumber"] for m in sqs.messages] == ["100", "200"]
    assert {m["queue_url"] for m in sqs.messages} == {"https://sqs.invalid/queue"}


def test_a_send_failure_is_absorbed_and_counted_separately() -> None:
    """The record was already skipped; raising here would redeliver good records."""
    sqs = RecordingSqsClient(
        error=ClientError({"Error": {"Code": "AccessDenied", "Message": "no"}}, "SendMessage")
    )

    sent = batch_item_failures.send_discarded_records(
        sqs_client=sqs,
        queue_url="https://sqs.invalid/queue",
        event=kinesis_event(["100"], payloads=[b"not json"]),
        sequence_numbers=["100"],
    )

    assert sent == 0
    assert sqs.messages == []


def test_nothing_is_sent_when_no_queue_is_configured() -> None:
    sqs = RecordingSqsClient()

    sent = batch_item_failures.send_discarded_records(
        sqs_client=sqs,
        queue_url="",
        event=kinesis_event(["100"], payloads=[b"not json"]),
        sequence_numbers=["100"],
    )

    assert sent == 0
    assert sqs.messages == []


def test_nothing_is_sent_when_no_records_were_discarded() -> None:
    sqs = RecordingSqsClient()

    sent = batch_item_failures.send_discarded_records(
        sqs_client=sqs,
        queue_url="https://sqs.invalid/queue",
        event=kinesis_event(["100"]),
        sequence_numbers=[],
    )

    assert sent == 0
    assert sqs.messages == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
