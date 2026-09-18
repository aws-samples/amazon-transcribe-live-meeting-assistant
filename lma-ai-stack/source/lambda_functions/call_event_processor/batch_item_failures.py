#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""Kinesis partial batch failure reporting for the call event processor.

The shared ``TranscriptBatchProcessor`` in the transcript enrichment Lambda
layer collects the exceptions raised while processing a Kinesis batch, but not
the records that produced them. :class:`SequenceTrackingBatchProcessor` keeps
that association so the handler can answer the event source mapping with a
``batchItemFailures`` response instead of an opaque success or a whole-batch
exception.

Failure policy
--------------
A Kinesis partial batch response is not a set of per-record acknowledgements.
Lambda takes the *lowest* sequence number in ``batchItemFailures``, moves the
shard checkpoint there, and redelivers that record and every record after it.
A reported record therefore holds up its shard until it succeeds or ages out,
so what gets reported is chosen as follows:

* **Transient failures are reported.** An AppSync, Comprehend, DynamoDB or
  downstream Lambda error is expected to succeed on redelivery, which is what
  the event source mapping's ``MaximumRetryAttempts``, bisect-on-error and
  on-failure destination are configured for.
* **Records that cannot be decoded or mapped are not reported.** A payload that
  is not JSON, or is JSON of the wrong shape, will fail identically on every
  redelivery. Reporting it would stall the shard for the full record age,
  delaying every later meeting on that shard. Such records are logged with
  their sequence number and skipped, so they can be found and replayed
  deliberately.
* **Failures that cannot be attributed to one record are reported as the whole
  batch.** If the batch handler itself raised, or the AppSync session could not
  be opened, the earliest record in the batch is reported so the entire batch is
  redelivered.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Tuple

# third-party imports from Lambda layer
from aws_lambda_powertools import Logger

# imports from Lambda layer
# pylint: disable=import-error
from transcript_batch_processor import TranscriptBatchProcessor

# pylint: enable=import-error

LOGGER = Logger(child=True, location="%(filename)s:%(lineno)d - %(funcName)s()")


def record_sequence_number(record: Any) -> str:
    """Returns the Kinesis sequence number of a batch record.

    Accepts either a Powertools ``KinesisStreamRecord`` or the raw event
    dictionary, so the caller does not depend on which of the two the batch
    processor hands out.
    """
    try:
        return str(record.kinesis.sequence_number)
    except AttributeError:
        pass
    try:
        return str(record["kinesis"]["sequenceNumber"])
    except (TypeError, KeyError, IndexError):
        return ""


def sequence_numbers_in_batch(event: Any) -> List[str]:
    """Returns the batch's sequence numbers in the order Lambda delivered them."""
    records = event.get("Records", []) if isinstance(event, dict) else []
    numbers = [record_sequence_number(record) for record in records]
    return [number for number in numbers if number]


def _sort_key(sequence_number: str) -> Tuple[int, Any]:
    """Orders sequence numbers numerically, falling back to a string compare.

    Kinesis sequence numbers are decimal integers of varying length, so a plain
    string comparison would put "10" before "9".
    """
    try:
        return (0, int(sequence_number))
    except ValueError:
        return (1, sequence_number)


def select_checkpoint(
    batch_sequence_numbers: Iterable[str],
    failing_sequence_numbers: Iterable[str],
) -> Optional[str]:
    """Returns the sequence number the shard should resume from, or ``None``.

    Lambda resumes a Kinesis shard from the lowest reported sequence number and
    redelivers everything from there, so reporting that one number is equivalent
    to reporting every failing record after it. Delivery order is used where
    possible because it is the shard's own ordering; the numeric comparison is
    only a fallback for a reported number that is not in the delivered batch.
    """
    failing = {number for number in failing_sequence_numbers if number}
    if not failing:
        return None
    for sequence_number in batch_sequence_numbers:
        if sequence_number in failing:
            return sequence_number
    return sorted(failing, key=_sort_key)[0]


def build_response(
    batch_sequence_numbers: Iterable[str],
    failing_sequence_numbers: Iterable[str],
) -> Dict[str, List[Dict[str, str]]]:
    """Builds the ``ReportBatchItemFailures`` response for a Kinesis batch."""
    checkpoint = select_checkpoint(batch_sequence_numbers, failing_sequence_numbers)
    if checkpoint is None:
        return {"batchItemFailures": []}
    return {"batchItemFailures": [{"itemIdentifier": checkpoint}]}


class SequenceTrackingBatchProcessor(TranscriptBatchProcessor):
    """Batch processor that remembers which Kinesis records failed.

    Behaves exactly like the layer's ``TranscriptBatchProcessor`` — the same
    records are processed and the same errors are collected — and additionally
    tracks the sequence number behind each failure so
    :attr:`batch_item_failures_response` can be returned to the event source
    mapping.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._batch_sequence_numbers: List[str] = []
        # (decoded payload, sequence number) for every record that decoded, so a
        # failing mutation can be matched back to its record by object identity.
        self._decoded_records: List[Tuple[Any, str]] = []
        self._retryable_sequence_numbers: List[str] = []
        self._discarded_sequence_numbers: List[str] = []
        self._has_unattributed_failure = False
        # The base class gathers the mutation coroutines with
        # return_exceptions=True and records the exceptions itself. Wrapping the
        # caller's function lets us note which record raised without changing
        # that accounting: the exception is re-raised untouched.
        self._wrapped_api_mutation_fn = self._api_mutation_fn
        self._api_mutation_fn = self._tracked_api_mutation

    @staticmethod
    def _map_kds_processed_message(message: Tuple) -> Dict[str, object]:
        mapped = TranscriptBatchProcessor._map_kds_processed_message(message=message)
        mapped["sequence_number"] = record_sequence_number(message[2])
        return mapped

    async def handle_event(self, event) -> None:
        """Handles a Kinesis batch, recording which records could not be decoded."""
        self._batch_sequence_numbers = sequence_numbers_in_batch(event)
        await super().handle_event(event=event)

        self._decoded_records = [
            (message["result"], str(message["sequence_number"]))
            for message in self._kds_processed_messages
            if message["status"] == "success"
        ]

        decode_failures = self._kds_batch_processor.response().get("batchItemFailures", [])
        self._discarded_sequence_numbers = [
            str(failure["itemIdentifier"])
            for failure in decode_failures
            if failure.get("itemIdentifier")
        ]
        if self._discarded_sequence_numbers:
            LOGGER.error(
                "discarding Kinesis records that could not be decoded; they are not "
                "reported as batch item failures because redelivery would fail the "
                "same way and hold up the rest of the shard",
                extra=dict(sequence_numbers=self._discarded_sequence_numbers),
            )

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        result = await super().__aexit__(exc_type, exc_val, exc_tb)
        # Any error the layer recorded beyond the ones we attributed came from
        # the batch as a whole (the handler body raised, or the AppSync session
        # could not be opened), so the whole batch has to be redelivered.
        attributed = len(self._retryable_sequence_numbers) + len(self._discarded_sequence_numbers)
        if len(self._errors) > attributed:
            self._has_unattributed_failure = True
        return result

    async def _tracked_api_mutation(self, **kwargs):
        try:
            return await self._wrapped_api_mutation_fn(**kwargs)
        except Exception:
            self._note_failed_message(kwargs.get("message"))
            raise

    def _note_failed_message(self, message: Any) -> None:
        sequence_number = self._sequence_number_for(message)
        if sequence_number is None:
            self._has_unattributed_failure = True
            return
        self._retryable_sequence_numbers.append(sequence_number)

    def _sequence_number_for(self, message: Any) -> Optional[str]:
        # Identity, not equality: two records in a batch can carry identical
        # payloads, and each decoded payload object is unique to its record.
        for decoded, sequence_number in self._decoded_records:
            if decoded is message:
                return sequence_number
        return None

    @property
    def discarded_sequence_numbers(self) -> List[str]:
        """Sequence numbers dropped as permanently undecodable."""
        return list(self._discarded_sequence_numbers)

    @property
    def failing_sequence_numbers(self) -> List[str]:
        """Sequence numbers that should be redelivered."""
        failing = list(self._retryable_sequence_numbers)
        if self._has_unattributed_failure and self._batch_sequence_numbers:
            failing.append(self._batch_sequence_numbers[0])
        return failing

    @property
    def has_unreportable_failure(self) -> bool:
        """True when a failure occurred that no sequence number can express."""
        return self._has_unattributed_failure and not self._batch_sequence_numbers

    @property
    def batch_item_failures_response(self) -> Dict[str, List[Dict[str, str]]]:
        """Partial batch response for the Kinesis event source mapping."""
        return build_response(self._batch_sequence_numbers, self.failing_sequence_numbers)
