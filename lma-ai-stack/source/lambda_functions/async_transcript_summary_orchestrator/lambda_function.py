#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

import json
import time
from os import getenv
from typing import TYPE_CHECKING, Any, Dict

import boto3

# third-party imports from Lambda layer
from aws_lambda_powertools import Logger
from aws_lambda_powertools.utilities.typing import LambdaContext
from botocore.config import Config as BotoCoreConfig
from botocore.exceptions import ClientError
from eventprocessor_utils import get_meeting_ttl

# pylint: enable=import-error
LOGGER = Logger(location="%(filename)s:%(lineno)d - %(funcName)s()")

if TYPE_CHECKING:
    from boto3 import Session as Boto3Session
    from mypy_boto3_kinesis.client import KinesisClient
    from mypy_boto3_lambda.client import LambdaClient
else:
    Boto3Session = object
    LambdaClient = object
    KinesisClient = object

BOTO3_SESSION: Boto3Session = boto3.Session()
KINESIS_CLIENT: KinesisClient = BOTO3_SESSION.client("kinesis")

TRANSCRIPT_SUMMARY_FUNCTION_ARN = getenv("TRANSCRIPT_SUMMARY_FUNCTION_ARN", "")
CALL_DATA_STREAM_NAME = getenv("CALL_DATA_STREAM_NAME", "")
SPEAKER_NAME_RESOLVER_ARN = getenv("SPEAKER_NAME_RESOLVER_ARN", "")

# Its own client, because the defaults are wrong for this one call. The resolver
# can run for its whole timeout, and a shared client's 60s socket timeout plus
# three retries would turn one slow pass into four complete passes -- four model
# calls, four writes racing each other. One attempt, and a socket timeout the
# callee cannot outlive.
SPEAKER_NAME_CLIENT_CONFIG = BotoCoreConfig(
    read_timeout=int(getenv("SPEAKER_NAME_READ_TIMEOUT", "75")),
    retries={"mode": "standard", "max_attempts": 1},
)
SPEAKER_NAME_CLIENT: LambdaClient = BOTO3_SESSION.client(
    "lambda",
    config=SPEAKER_NAME_CLIENT_CONFIG,
)


# Its own client too, for the same reason as the speaker-naming one. Summarising
# a long meeting calls a model once per prompt template and can take longer than
# a 60s socket timeout, and with three attempts each such run would be abandoned
# and started again until the attempts ran out, with nothing written. One
# attempt, and a socket timeout that leaves this function -- whose own timeout is
# the 900s Lambda maximum -- time to record the outcome after the speaker-naming
# pass has had its 75s. The template sets both; a test holds their sum below
# the function's timeout.
SUMMARY_CLIENT_CONFIG = BotoCoreConfig(
    read_timeout=int(getenv("SUMMARY_READ_TIMEOUT", "780")),
    retries={"mode": "standard", "max_attempts": 1},
)
SUMMARY_CLIENT: LambdaClient = BOTO3_SESSION.client(
    "lambda",
    config=SUMMARY_CLIENT_CONFIG,
)

# Retried here rather than by the client, and only when Lambda refused to start
# the summary function: that is the one failure after which the callee is known
# not to be running, so trying again cannot produce a second summary.
SUMMARY_THROTTLE_ATTEMPTS = 3
SUMMARY_THROTTLE_BACKOFF_SECONDS = 2.0
THROTTLE_ERROR_CODES = frozenset({"TooManyRequestsException"})

# What is written as the summary when the summary function could not be run, or
# answered with nothing readable. The same placeholder the Bedrock summariser
# returns when it fails, so the event processor treats both alike: it stores no
# text and marks the meeting's summary as failed.
FAILED_SUMMARY_TEXT = "An error occurred."


def _invoke_summary_function(message: Dict[str, Any]) -> Dict[str, Any]:
    for attempt in range(SUMMARY_THROTTLE_ATTEMPTS):
        try:
            return SUMMARY_CLIENT.invoke(
                FunctionName=TRANSCRIPT_SUMMARY_FUNCTION_ARN,
                InvocationType="RequestResponse",
                Payload=json.dumps(message),
            )
        except ClientError as error:
            code = error.response.get("Error", {}).get("Code")
            if code not in THROTTLE_ERROR_CODES or attempt + 1 == SUMMARY_THROTTLE_ATTEMPTS:
                raise
            LOGGER.warning("The summary function was throttled; trying again")
            time.sleep(SUMMARY_THROTTLE_BACKOFF_SECONDS * 2**attempt)
    raise RuntimeError("unreachable")  # pragma: no cover - the loop returns or raises


def get_call_summary(message: Dict[str, Any]) -> str:
    """The summary the configured summary function produced, or the placeholder.

    Never raises. The meeting is marked "summary in progress" when it ends, and
    only the summary event this function's caller writes clears that mark; a
    failure that escaped from here would leave the meeting page waiting for a
    summary that is not coming, and would have Lambda run this whole function --
    the speaker-naming pass included -- again.
    """
    try:
        lambda_response = _invoke_summary_function(message)
        body = json.loads(lambda_response.get("Payload").read().decode("utf-8"))
    except Exception:  # pylint: disable=broad-except
        LOGGER.exception(
            "The transcript summary function failed or returned something unreadable",
            extra=dict(call_id=message.get("CallId")),
        )
        return FAILED_SUMMARY_TEXT
    summary = body.get("summary") if isinstance(body, dict) else None
    if not isinstance(summary, str):
        LOGGER.error(
            "The transcript summary function returned no summary",
            extra=dict(call_id=message.get("CallId")),
        )
        return FAILED_SUMMARY_TEXT
    return summary


def write_call_summary_to_kds(message: Dict[str, Any]):
    callId = message.get("CallId", None)
    expiresAfter = message.get("ExpiresAfter", get_meeting_ttl())

    new_message = dict(
        CallId=callId,
        EventType="ADD_SUMMARY",
        ExpiresAfter=expiresAfter,
        CallSummaryText=message["CallSummaryText"],
    )

    if callId:
        try:
            KINESIS_CLIENT.put_record(
                StreamName=CALL_DATA_STREAM_NAME, PartitionKey=callId, Data=json.dumps(new_message)
            )
            LOGGER.info("Write ADD_SUMMARY event to KDS")
        except Exception as error:
            LOGGER.error(
                "Error writing ADD_SUMMARY event to KDS ",
                extra=error,
            )
    return


def name_speakers(call_id: str) -> None:
    """Name the separated voices before the transcript is summarised.

    Synchronous and ordered ahead of the summary on purpose: the summary reads
    the transcript through FetchTranscript, which resolves stored speaker names,
    so running this first is what gets "Sarah Lee" into the summary instead of
    "Other Participant (spk_0)".

    This runs off the Kinesis path -- this function is itself invoked
    asynchronously -- so a slow model call delays only this meeting's summary and
    does not hold up live transcription for anybody.

    Naming is an enhancement; a summary must still be produced if it fails.
    """
    if not SPEAKER_NAME_RESOLVER_ARN or not call_id:
        return
    try:
        SPEAKER_NAME_CLIENT.invoke(
            FunctionName=SPEAKER_NAME_RESOLVER_ARN,
            InvocationType="RequestResponse",
            # Marks this as the once-per-meeting pass, which skips the
            # cooldown and the "has anybody spoken since" gate -- both of those
            # exist to pace a browser timer, and this is the pass the summary
            # depends on.
            Payload=json.dumps({"CallId": call_id, "Trigger": "meeting-end"}),
        )
        LOGGER.debug("Ran the speaker name resolver before summarising")
    except Exception:  # pylint: disable=broad-except
        LOGGER.exception(
            "Speaker naming failed; summarising with whatever names are stored",
            extra=dict(call_id=call_id),
        )


@LOGGER.inject_lambda_context
def handler(event, context: LambdaContext):
    # pylint: disable=unused-argument
    """Lambda handler"""
    LOGGER.debug("Transcript summary lambda event", extra={"event": event})

    data = json.loads(json.dumps(event))

    name_speakers(data.get("CallId", ""))

    data["CallSummaryText"] = get_call_summary(message=data)

    write_call_summary_to_kds(data)
