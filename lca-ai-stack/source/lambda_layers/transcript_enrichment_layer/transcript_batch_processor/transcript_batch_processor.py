# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
""" Transcript Batch Processor
"""
import asyncio
import traceback
from typing import Any, Coroutine, Dict, List, Literal, Optional, Protocol, Tuple, Union

# third-party imports from Lambda layer
from aws_lambda_powertools import Logger
from aws_lambda_powertools.utilities.data_classes import KinesisStreamEvent
from aws_lambda_powertools.utilities.data_classes.kinesis_stream_event import KinesisStreamRecord
from aws_lambda_powertools.utilities.batch import BatchProcessor, EventType

from gql.client import AsyncClientSession

# module imports from Lambda layer
# pylint: disable=import-error
from appsync_utils import AppsyncAioGqlClient

# pylint: enable=import-error


LOGGER = Logger(child=True, location="%(filename)s:%(lineno)d - %(funcName)s()")
KDS_BATCH_PROCESSOR = BatchProcessor(event_type=EventType.KinesisDataStreams)


class TranscriptBatchProcessor:
    """Call Transcript Batch Processor"""

    # pylint: disable=too-many-instance-attributes
    class ApiMutationFnType(Protocol):
        """Api Mutation Function Signature"""

        # pylint: disable=too-few-public-methods
        def __call__(
            self,
            message: object,
            settings: Dict[str, Any],
            appsync_session: AsyncClientSession,
            sns_client: object,
            agent_assist_args: Dict[str, object],
            sentiment_analysis_args: Dict[str, object]
        ) -> Coroutine[Any, Any, Any]:
            ...

    def __init__(
        self,
        appsync_client: AppsyncAioGqlClient,
        api_mutation_fn: ApiMutationFnType,
        sns_client,
        settings: Dict[str, Any],
        agent_assist_args: Optional[Dict[str, Any]] = None,
        sentiment_analysis_args: Optional[Dict[str, object]] = None
    ):
        self._appsync_client = appsync_client
        self._sns_client = sns_client
        self._settings = settings
        self._api_mutation_fn = api_mutation_fn
        self._agent_assist_args = agent_assist_args or {}
        self._sentiment_analysis_args = sentiment_analysis_args or {}
        self._kds_batch_processor = KDS_BATCH_PROCESSOR

        self._kds_processed_messages: List[Dict[str, object]] = []
        self._successes: List = []
        self._errors: List = []
        self._has_error: bool = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> Literal[True]:
        if exc_type or exc_val or exc_tb:
            LOGGER.error(
                "transcript batch processor error: %s, %s, %s",
                exc_type,
                exc_val,
                traceback.format_tb(exc_tb),
            )
            self._has_error = True
            self._errors.append(exc_val)
        try:
            async with self._appsync_client as appsync_session:
                coroutines = [
                    self._api_mutation_fn(
                        message=message["result"],
                        settings=self._settings,
                        appsync_session=appsync_session,
                        sns_client=self._sns_client,
                        agent_assist_args=self._agent_assist_args,
                        sentiment_analysis_args=self._sentiment_analysis_args,
                    )
                    for message in self._kds_processed_messages
                    if message["status"] == "success"
                ]

                results: List[Union[Dict, Exception]] = await asyncio.gather(
                    *coroutines,
                    return_exceptions=True,
                )
                for result in results:
                    if isinstance(result, Exception):
                        LOGGER.error("transcript api mutation exception: %s", result)
                        self._has_error = True
                        self._errors.append(result)
                    else:
                        self._successes.append(result)
        except Exception as exception:  # pylint: disable=broad-except
            self._has_error = True
            self._errors.append(exception)
            LOGGER.exception("transcript batch processor exception: %s", exception)

        return True

    @staticmethod
    def _map_kds_processed_message(
        message: Tuple,
    ) -> Dict[str, object]:
        status: Literal["success", "fail"] = message[0]
        LOGGER.debug("status", extra=dict(status=status))
        result: Any = message[1]
        LOGGER.debug("result", extra=dict(result=result))
        record: KinesisStreamRecord = message[2]
        LOGGER.debug("record", extra=dict(record=record))

        return dict(
            status=status,
            result=result,
        )

    @staticmethod
    def _process_record(record: KinesisStreamRecord) -> Dict:
        payload: Dict = record.kinesis.data_as_json()
        LOGGER.debug("payload", extra=dict(payload=payload))

        return payload

    async def handle_event(self, event: KinesisStreamEvent):
        """Handles Call Transcript Events"""
        batch = event["Records"]
        with self._kds_batch_processor(records=batch, handler=self._process_record):
            self._kds_processed_messages = [
                self._map_kds_processed_message(message=message)
                for message in self._kds_batch_processor.process()
            ]

        kds_batch_processor_response = self._kds_batch_processor.response()

        failures = kds_batch_processor_response.get("batchItemFailures")
        if failures:
            self._has_error = True
            self._errors.extend(
                [
                    TypeError(f"unable to decode or transform Kinesis record: {failure}")
                    for failure in failures
                ]
            )
            LOGGER.error("failed to decode or map KDS records", extra=dict(failures=failures))

    @property
    def results(self):
        """Processor Results"""
        return dict(
            successes=self._successes,
            errors=self._errors,
        )
