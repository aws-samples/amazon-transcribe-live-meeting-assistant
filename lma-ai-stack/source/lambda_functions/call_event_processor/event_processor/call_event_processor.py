# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""Transcribe API Mutation Processor"""

import asyncio
import json
import re
from datetime import datetime, timezone
from functools import lru_cache
from os import getenv
from statistics import fmean
from types import MappingProxyType
from typing import (
    TYPE_CHECKING,
    Any,
    Coroutine,
    Dict,
    List,
    Literal,
    Mapping,
    Optional,
    Pattern,
    Tuple,
)

# third-party imports from Lambda layer
import boto3

# custom utils/helpers imports from Lambda layer
# pylint: disable=import-error
from appsync_utils import execute_gql_query_with_retries
from aws_lambda_powertools import Logger
from botocore.config import Config as BotoCoreConfig
from eventprocessor_utils import (
    get_meeting_ttl,
    get_owner_from_jwt,
    get_transcription_ttl,
    normalize_transcript_segments,
    transform_segment_to_add_sentiment,
)
from gql.client import AsyncClientSession as AppsyncAsyncClientSession
from gql.dsl import DSLMutation, DSLQuery, DSLSchema, dsl_gql
from graphql.language.printer import print_ast
from graphql_helpers import (
    call_fields,
    describe_exception,
    graphql_error_messages,
    optional_graphql_field,
    require_graphql_field,
    traceback_frames,
    transcript_segment_fields,
    transcript_segment_sentiment_fields,
    video_keyframe_fields,
)

# Validation of ADD_VIDEO_KEYFRAME, AWS-free for the same reason as pure.py below.
from keyframe_input import KeyframeEventError, build_keyframe_input  # noqa: E402
from lma_channels import canonical_channel, is_speech_channel  # noqa: E402
from lma_config import (  # noqa: E402
    MEETING_AGENDA,
    MEETING_ASSIST,
    MEETING_PROCESSING,
    read_setting,
    shipped_setting_defaults,
)
from metrics_utils import catalogue as metric_names
from metrics_utils.emf import METRICS

# The AWS-free computation, split out so it can be tested without importing this
# module (which needs aiohttp, powertools and the layer on the path). See pure.py.
from pure import (  # noqa: E402
    ChannelType,
    Sentiment,
    SentimentByPeriodEntry,
    SentimentEntry,
    SentimentPerChannel,
    agenda_derivation_due,
    apply_meeting_access,
    capture_context_fields,
    convert_keys_to_uppercamelcase,
    get_sentiment_per_quarter,
    merge_dicts,
    segment_analyses,
)

# pylint: enable=import-error
if TYPE_CHECKING:
    from boto3 import Session as Boto3Session
    from mypy_boto3_lambda.client import LambdaClient
    from mypy_boto3_lambda.type_defs import InvocationResponseTypeDef
else:
    LambdaClient = object
    InvocationResponseTypeDef = object
    Boto3Session = object

IS_SENTIMENT_ANALYSIS_ENABLED = getenv("IS_SENTIMENT_ANALYSIS_ENABLED", "true").lower() == "true"

BOTO3_SESSION: Boto3Session = boto3.Session()
CLIENT_CONFIG = BotoCoreConfig(
    retries={"mode": "adaptive", "max_attempts": 3},
)
TRANSCRIPT_LAMBDA_HOOK_FUNCTION_ARN = getenv("TRANSCRIPT_LAMBDA_HOOK_FUNCTION_ARN", "")

START_OF_CALL_LAMBDA_HOOK_FUNCTION_ARN = getenv("START_OF_CALL_LAMBDA_HOOK_FUNCTION_ARN", "")
POST_CALL_SUMMARY_LAMBDA_HOOK_FUNCTION_ARN = getenv(
    "POST_CALL_SUMMARY_LAMBDA_HOOK_FUNCTION_ARN", ""
)

ASYNC_TRANSCRIPT_SUMMARY_ORCHESTRATOR_ARN = getenv("ASYNC_TRANSCRIPT_SUMMARY_ORCHESTRATOR_ARN", "")
SPEAKER_NAME_RESOLVER_ARN = getenv("SPEAKER_NAME_RESOLVER_ARN", "")
# Deletes a meeting's transcript once it is summarized, when the Retention
# section says to; told when each meeting ends and when its summary is stored.
TRANSCRIPT_DELETION_FUNCTION_ARN = getenv("TRANSCRIPT_DELETION_FUNCTION_ARN", "")
# The automatic agenda pass: a meeting with no agenda is read once, about
# agenda_derivation_threshold() seconds in, for an agenda somebody stated. 0
# turns it off. See agenda_deriver.
AGENDA_DERIVER_ARN = getenv("AGENDA_DERIVER_ARN", "")
# Reads the text of each key frame, once, after it is stored. See
# keyframe_text_extractor.
KEYFRAME_TEXT_EXTRACTOR_ARN = getenv("KEYFRAME_TEXT_EXTRACTOR_ARN", "")
#: The threshold used when the Meeting Agenda section cannot be read: 300
#: seconds, the setting's shipped default in setting_defaults.json.
AGENDA_DERIVATION_AFTER_SECONDS = 300.0
# Meetings this execution environment has already queued the pass for; see
# agenda_derivation_due for why this is not what makes the pass run once.
AGENDA_PASS_QUEUED: set = set()
IS_TRANSCRIPT_SUMMARY_ENABLED = getenv("IS_TRANSCRIPT_SUMMARY_ENABLED", "false").lower() == "true"

ASYNC_AGENT_ASSIST_ORCHESTRATOR_ARN = getenv("ASYNC_AGENT_ASSIST_ORCHESTRATOR_ARN", "")


def transcript_hook_nonpartial_only() -> bool:
    """Whether the transcript Lambda hook is sent finished segments only.

    The Meeting Processing section's value on the Configuration page, else the
    shipped default in setting_defaults.json.
    """
    return read_setting(
        MEETING_PROCESSING.name,
        "transcriptHookNonPartialOnly",
        default=True,
    )


def agenda_derivation_threshold() -> float:
    """Seconds into a meeting with no agenda at which it is read for one; 0 is off.

    The Meeting Agenda section's value on the Configuration page, else the
    shipped default in setting_defaults.json. Read on the section's standard
    tier, so a saved change reaches meetings about five minutes later.
    """
    return float(
        read_setting(
            MEETING_AGENDA.name,
            "autoDeriveAfterSeconds",
            default=AGENDA_DERIVATION_AFTER_SECONDS,
        )
    )


@lru_cache(maxsize=8)
def _compiled_wake_phrase(pattern: str) -> Optional[Pattern[str]]:
    """``pattern`` compiled, or None when it is not a regular expression."""
    try:
        return re.compile(pattern)
    except re.error as error:
        LOGGER.warning(
            "the configured wake phrase is not a regular expression (%s); using the "
            "shipped pattern until it is corrected",
            error,
        )
        return None


def wake_phrase_pattern() -> Optional[Pattern[str]]:
    """The regular expression that wakes the meeting assistant.

    The Meeting Assistant section of the Configuration page, with its shipped
    default. When the saved pattern does not compile, the shipped one is used
    until it is corrected.
    """
    configured = read_setting(MEETING_ASSIST.name, "wakePhraseRegex")
    compiled = _compiled_wake_phrase(configured) if configured else None
    if compiled is not None:
        return compiled
    return _compiled_wake_phrase(shipped_setting_defaults()[MEETING_ASSIST.name]["wakePhraseRegex"])


if (
    TRANSCRIPT_LAMBDA_HOOK_FUNCTION_ARN
    or ASYNC_TRANSCRIPT_SUMMARY_ORCHESTRATOR_ARN
    or SPEAKER_NAME_RESOLVER_ARN
    or TRANSCRIPT_DELETION_FUNCTION_ARN
    or AGENDA_DERIVER_ARN
    or KEYFRAME_TEXT_EXTRACTOR_ARN
    or ASYNC_AGENT_ASSIST_ORCHESTRATOR_ARN
    or START_OF_CALL_LAMBDA_HOOK_FUNCTION_ARN
    or POST_CALL_SUMMARY_LAMBDA_HOOK_FUNCTION_ARN
):
    LAMBDA_HOOK_CLIENT: LambdaClient = BOTO3_SESSION.client("lambda", config=CLIENT_CONFIG)


IS_LAMBDA_AGENT_ASSIST_ENABLED = False

LOGGER = Logger(location="%(filename)s:%(lineno)d - %(funcName)s()")
EVENT_LOOP = asyncio.get_event_loop()

CALL_EVENT_TYPE_TO_STATUS = {
    "START": "STARTED",
    "END": "ENDED",
    "ADD_S3_RECORDING_URL": "ENDED",
    "ADD_S3_VIDEO_RECORDING_URL": "ENDED",
}

CUSTOMER_PHONE_NUMBER = ""
CALL_ID = ""

CALL_DATA_STREAM_NAME = getenv("CALL_DATA_STREAM_NAME", "")

# The two messages a resolver raises when DynamoDB rejects its condition. Most
# of them share ``source/appsync/dDbPutCondition.response.vtl``, which raises the
# first; ``createCall`` writes transactionally and has its own
# ``source/appsync/createCall.response.vtl``, which raises the second.
PUT_CONDITION_FAILURE_MESSAGES = frozenset(
    {
        "item put condition failure",
        "Item already exists",
    }
)

# Stands in for a createCall result when the meeting was already created, so the
# START branch can tell a first delivery from a redelivered one and invoke the
# start-of-call hook once per meeting. Read-only rather than documented as such:
# it is module state that ends up in the batch's results.
CALL_ALREADY_CREATED: Mapping[str, Any] = MappingProxyType({"ok": True, "AlreadyExists": True})


def is_put_condition_failure(error: Any) -> bool:
    """Whether a write was rejected by its resolver's own condition.

    Passed to ``execute_gql_query_with_retries`` as its
    ``should_ignore_exception_fn``. What a rejection means is per resolver, and on
    each of the three live call sites it means the row already holds a version of
    this write that should not be replaced, so retrying buys nothing
    (``execute_update_call_aggregation_mutation`` passes it too but has no
    callers):

    * ``createCall`` writes with ``attribute_not_exists(#PK)``, so a rejection is
      a meeting that is already created -- a redelivered START event.
    * ``addTranscriptSegment`` conditions only a *partial* segment, on
      ``attribute_not_exists(IsPartial) OR IsPartial = :true``, so a rejection is
      a partial arriving after the final segment it would have overwritten.
    * ``updateCallAggregation`` writes through ``updateCall.request.vtl``, whose
      condition is ``attribute_exists(#PK) AND #UpdatedAt < :UpdatedAt``, so a
      rejection is an aggregation already superseded by a newer one -- which the
      concurrent writes for one Kinesis batch produce routinely, because a later
      segment's aggregation can land first. Dropping the superseded write is
      intended: the one that won already covers the same segments, and the next
      non-partial segment aggregates again. The same condition is rejected when
      the meeting row is not there at all, and dropping that is harmless for a
      different reason -- ``get_call_details`` reads the meeting earlier in the
      same record and has already failed it on a null ``getCall``, so the
      aggregation write is not reached.

    The messages are read off whatever the transport raised -- gql keeps them in
    an ``errors`` attribute on the exception, and a bare error mapping is
    accepted as well. Any other shape yields no messages, so an unrecognised
    failure is retried and reported rather than ignored, and nothing here raises:
    the retry wrapper calls this from inside an ``except``.
    """
    return any(
        message in PUT_CONDITION_FAILURE_MESSAGES for message in graphql_error_messages(error)
    )


##########################################################################
# Transcripts
##########################################################################


def add_transcript_segments(
    message: Dict[str, Any],
    meeting: Dict[str, Any],
    appsync_session: AppsyncAsyncClientSession,
) -> List[Coroutine]:
    """Add Transcript Segment GraphQL Mutation

    The segment takes the meeting's Owner and SharedWith (``meeting`` is what
    get_call_details read), set on ``message`` itself so that the sentiment
    write of the same segment, which replaces the item, carries them too.
    """
    if not appsync_session.client.schema:
        raise ValueError("invalid AppSync schema")
    schema = DSLSchema(appsync_session.client.schema)

    tasks = []
    if message:
        if "OriginalTranscript" not in message:
            message["OriginalTranscript"] = message["Transcript"]

        apply_meeting_access(message, meeting)

        query = dsl_gql(
            DSLMutation(
                schema.Mutation.addTranscriptSegment.args(input=message).select(
                    *transcript_segment_fields(schema),
                )
            )
        )

        tasks.append(
            execute_gql_query_with_retries(
                query,
                client_session=appsync_session,
                logger=LOGGER,
                should_ignore_exception_fn=is_put_condition_failure,
            ),
        )

    return tasks


async def add_sentiment_to_transcript(
    message: Dict[str, Any],
    sentiment_analysis_args: Dict[str, Any],
    appsync_session: AppsyncAsyncClientSession,
):
    if not appsync_session.client.schema:
        raise ValueError("invalid AppSync schema")
    schema = DSLSchema(appsync_session.client.schema)

    transcript_segment_with_sentiment = await transform_segment_to_add_sentiment(
        message, sentiment_analysis_args
    )

    result = {}
    query = dsl_gql(
        DSLMutation(
            schema.Mutation.addTranscriptSegment.args(
                input=transcript_segment_with_sentiment
            ).select(
                *transcript_segment_fields(schema),
                *transcript_segment_sentiment_fields(schema),
            )
        )
    )

    result = await execute_gql_query_with_retries(
        query,
        client_session=appsync_session,
        logger=LOGGER,
    )

    return result


def add_transcript_sentiment_analysis(
    message: Dict[str, Any],
    sentiment_analysis_args: Dict[str, Any],
    appsync_session: AppsyncAsyncClientSession,
) -> List[Coroutine]:
    """Add Transcript Sentiment GraphQL Mutation"""

    tasks = []

    task = add_sentiment_to_transcript(message, sentiment_analysis_args, appsync_session)
    tasks.append(task)

    return tasks


async def execute_create_call_mutation(
    message: Dict[str, Any],
    appsync_session: AppsyncAsyncClientSession,
) -> Dict:
    if not appsync_session.client.schema:
        raise ValueError("invalid AppSync schema")
    schema = DSLSchema(appsync_session.client.schema)

    global CUSTOMER_PHONE_NUMBER
    global CALL_ID
    CUSTOMER_PHONE_NUMBER = message.get("CustomerPhoneNumber", "")
    CALL_ID = message.get("CallId", "")

    owner = get_owner_from_jwt(message.get("AccessToken"))
    # If JWT decode fails (returns None), use AgentId as owner (for VP service calls)
    if owner is None:
        owner = message.get("AgentId", "system@lma.aws")
        print(f"Using AgentId as owner: {owner}")
    message.update({"Owner": owner})

    query = dsl_gql(
        DSLMutation(
            schema.Mutation.createCall.args(input=message).select(
                schema.CreateCallOutput.CallId, schema.CreateCallOutput.Owner
            )
        )
    )

    # A rejected condition here means the meeting is already created, and the
    # caller has to be able to tell that from having created it: a redelivered
    # START must not invoke the start-of-call hook a second time.
    result = await execute_gql_query_with_retries(
        query,
        client_session=appsync_session,
        logger=LOGGER,
        should_ignore_exception_fn=is_put_condition_failure,
        ignored_exception_response=CALL_ALREADY_CREATED,
    )

    query_string = print_ast(query)
    LOGGER.debug("query result", extra=dict(query=query_string, result=result))

    return result


# What the summary step stores when it could not produce a summary; the same
# list as summary_regenerator's UNUSABLE_SUMMARIES. Compared case-insensitively
# after stripping.
FAILED_SUMMARY_TEXTS = frozenset(
    {"", "an error occurred.", "an error occurred", "no summary available"}
)


def summary_was_produced(text: Any) -> bool:
    """Whether a stored summary is one, rather than a failed run's placeholder."""
    return isinstance(text, str) and text.strip().lower() not in FAILED_SUMMARY_TEXTS


# The summary state (Call.CallSummaryState) the end-of-meeting run writes: one
# JSON object, the same shape summary_regenerator writes for a regeneration and
# ui/src/components/common/summary-state.js reads. `trigger` tells the two runs
# apart, because the meeting page words them differently and gives up waiting
# for this one sooner.
SUMMARY_TRIGGER_MEETING_END = "meeting-end"
SUMMARY_FAILED_MESSAGE = "the summary could not be generated"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def summary_queued_state() -> str:
    """The state a meeting carries from the moment its summary is queued.

    Written with the ENDED status, in the same mutation, so there is no moment
    at which the meeting page sees an ended meeting with no summary and nothing
    to say why.
    """
    return json.dumps(
        {
            "source": "generated",
            "status": "running",
            "trigger": SUMMARY_TRIGGER_MEETING_END,
            "startedAt": _now_iso(),
        }
    )


def summary_outcome_state(text: Any) -> str:
    """The state written beside the end-of-meeting summary: done, or failed."""
    if summary_was_produced(text):
        return json.dumps(
            {
                "source": "generated",
                "status": "idle",
                "trigger": SUMMARY_TRIGGER_MEETING_END,
                "updatedAt": _now_iso(),
            }
        )
    return json.dumps(
        {
            "source": "generated",
            "status": "failed",
            "trigger": SUMMARY_TRIGGER_MEETING_END,
            "message": SUMMARY_FAILED_MESSAGE,
            "updatedAt": _now_iso(),
        }
    )


def was_already_created(create_call_result: Any) -> bool:
    """Whether a createCall result came from a meeting that already existed.

    True only for :data:`CALL_ALREADY_CREATED`, which
    :func:`execute_create_call_mutation` returns in place of a result when the
    resolver rejected its condition.
    """
    return bool(isinstance(create_call_result, Mapping) and create_call_result.get("AlreadyExists"))


async def execute_update_call_status_mutation(
    message: Dict[str, Any],
    appsync_session: AppsyncAsyncClientSession,
) -> Dict:
    status = CALL_EVENT_TYPE_TO_STATUS.get(message.get("EventType"))
    if not status:
        error_message = "unrecognized status from event type in update call"
        raise TypeError(error_message)

    if status == "STARTED":
        # STARTED status is set by createCall - skip update mutation
        return {"ok": True}

    if not appsync_session.client.schema:
        raise ValueError("invalid AppSync schema")
    schema = DSLSchema(appsync_session.client.schema)

    query = dsl_gql(
        DSLMutation(
            schema.Mutation.updateCallStatus.args(input={**message, "Status": status}).select(
                *call_fields(schema)
            )
        )
    )
    result = await execute_gql_query_with_retries(
        query,
        client_session=appsync_session,
        logger=LOGGER,
    )

    query_string = print_ast(query)
    LOGGER.debug("query result", extra=dict(query=query_string, result=result))

    return result


async def get_call_status(
    message: Dict[str, Any],
    appsync_session: AppsyncAsyncClientSession,
) -> Optional[str]:
    """Returns the meeting's current Status, or None if it could not be read.

    Used to tell a first END event from a redelivered one. Deliberately
    fail-open: if the status cannot be read the caller carries on as if the
    meeting were still open, because doing the end-of-meeting work twice is
    better than not doing it at all.
    """
    call_id = message.get("CallId")
    if not call_id or not appsync_session.client.schema:
        return None

    schema = DSLSchema(appsync_session.client.schema)
    query = dsl_gql(
        DSLQuery(
            schema.Query.getCall.args(CallId=call_id).select(
                schema.Call.CallId,
                schema.Call.Status,
            )
        )
    )
    try:
        result = await execute_gql_query_with_retries(
            query,
            client_session=appsync_session,
            logger=LOGGER,
        )
    except Exception as error:  # pylint: disable=broad-except
        # Frames and the reduced errors rather than a rendered traceback: nothing
        # above sees this exception, since the fail-open contract swallows it, so
        # this line is the whole record of it, and a rendered traceback would end
        # in the exception's own text.
        LOGGER.error(
            "could not read current status for call %s - error: [%s]",
            call_id,
            describe_exception(error),
            extra=dict(frames=traceback_frames(error)),
        )
        return None

    # Optional, not required: a null getCall or a response without one leaves the
    # status unknown, and an unknown status is what the fail-open contract above
    # is for. The shape that came back is logged by the helper.
    status = optional_graphql_field(result, "getCall", "Status", logger=LOGGER)
    LOGGER.debug("current call status", extra=dict(call_id=call_id, status=status))
    return status


async def execute_get_transcript_segments_query(
    message: Dict[str, Any],
    appsync_session: AppsyncAsyncClientSession,
) -> Dict:
    call_id = message.get("CallId")
    if not call_id:
        error_message = "callid does not exist"
        raise TypeError(error_message)

    if not appsync_session.client.schema:
        raise ValueError("invalid AppSync schema")
    schema = DSLSchema(appsync_session.client.schema)

    # A long meeting's segments span several pages of the table, and a page can
    # come back empty with a nextToken, so pages are read until there is none.
    # The pages are returned as one response of the query's shape. A page that
    # does not carry the list is returned as it came, for the caller to treat
    # as no answer.
    segments: List[Any] = []
    next_token: Optional[str] = None
    while True:
        query = dsl_gql(
            DSLQuery(
                schema.Query.getTranscriptSegmentsWithSentiment.args(
                    callId=call_id, nextToken=next_token
                ).select(
                    schema.TranscriptSegmentsWithSentimentList.TranscriptSegmentsWithSentiment.select(
                        schema.TranscriptSegmentWithSentiment.PK,
                        schema.TranscriptSegmentWithSentiment.SK,
                        schema.TranscriptSegmentWithSentiment.CallId,
                        schema.TranscriptSegmentWithSentiment.Channel,
                        schema.TranscriptSegmentWithSentiment.SegmentId,
                        schema.TranscriptSegmentWithSentiment.StartTime,
                        schema.TranscriptSegmentWithSentiment.EndTime,
                        schema.TranscriptSegmentWithSentiment.Sentiment,
                        schema.TranscriptSegmentWithSentiment.SentimentWeighted,
                    ),
                    schema.TranscriptSegmentsWithSentimentList.nextToken,
                )
            )
        )
        result = await execute_gql_query_with_retries(
            query,
            client_session=appsync_session,
            logger=LOGGER,
        )
        LOGGER.debug(
            "get transcript segments result", extra=dict(query=print_ast(query), result=result)
        )
        page = (result or {}).get("getTranscriptSegmentsWithSentiment")
        if not isinstance(page, dict) or not isinstance(
            page.get("TranscriptSegmentsWithSentiment"), list
        ):
            return result
        segments.extend(page["TranscriptSegmentsWithSentiment"])
        next_token = page.get("nextToken")
        if not next_token:
            break

    return {
        "getTranscriptSegmentsWithSentiment": {
            "TranscriptSegmentsWithSentiment": segments,
            "nextToken": None,
        }
    }


def latest_speech_end_seconds(segments: List[Any]) -> float:
    """The latest EndTime, in seconds, of the final speech segments listed.

    This is how long the meeting has run on the transcript's clock. A meeting's
    final segments arrive in more than one batch, and not in order, so the end
    time of the one segment a batch carries is not it: written as the duration,
    the value stored is whichever batch wrote last.
    """
    latest = 0.0
    for segment in segments:
        if not isinstance(segment, dict) or not is_speech_channel(segment.get("Channel")):
            continue
        try:
            latest = max(latest, float(segment.get("EndTime") or 0.0))
        except (TypeError, ValueError):
            continue
    return latest


async def get_aggregated_sentiment(
    message: Dict[str, Any],
    appsync_session: AppsyncAsyncClientSession,
) -> Optional[Sentiment]:
    """The meeting's sentiment aggregation, or None if the segments were unreadable."""
    sentiment, _latest_end = await get_sentiment_and_latest_end(
        message=message, appsync_session=appsync_session
    )
    return sentiment


async def get_sentiment_and_latest_end(
    message: Dict[str, Any],
    appsync_session: AppsyncAsyncClientSession,
) -> Tuple[Optional[Sentiment], float]:
    """The meeting's sentiment aggregation and the latest end of its final speech segments.

    Both come from one read of the meeting's final segments. The aggregation is
    None if the segments were unreadable, and the end time is then 0.0.
    """
    call_id = message.get("CallId")
    if not call_id:
        error_message = "callid does not exist"
        raise TypeError(error_message)

    if not appsync_session.client.schema:
        raise ValueError("invalid AppSync schema")
    DSLSchema(appsync_session.client.schema)

    try:
        result = await execute_get_transcript_segments_query(
            message=message, appsync_session=appsync_session
        )
    except Exception as error:  # pylint: disable=broad-except
        # The segments could not be read at all -- a response carrying errors
        # arrives from gql as a raised exception rather than as a null field.
        # Letting it out would fail the record, and the transcript segment goes
        # with it: its own write is gathered after this read. The aggregation is
        # recomputed on the next final segment, so it is the cheaper thing to
        # lose. `get_call_details` reads the same meeting earlier in this record
        # and would already have failed it if AppSync were unreachable.
        LOGGER.error(
            "could not read the scored segments for call %s; aggregating nothing - error: [%s]",
            call_id,
            describe_exception(error),
            extra=dict(frames=traceback_frames(error)),
        )
        return None, 0.0

    sentiment_entry_list_by_channel: Dict[ChannelType, SentimentPerChannel] = {}

    # An empty list is a valid answer -- the meeting has no scored segments yet
    # -- and aggregates to nothing. No list at all is not an answer: the caller
    # returns None for it so that nothing is written, because an empty
    # aggregation written with a fresh UpdatedAt passes updateCall's condition
    # and would replace the stored one. Failing instead would fail the record
    # that carried the segment, which is the outcome this read exists to avoid.
    segments = optional_graphql_field(
        result,
        "getTranscriptSegmentsWithSentiment",
        "TranscriptSegmentsWithSentiment",
        logger=LOGGER,
    )
    if segments is None:
        return None, 0.0
    latest_end = latest_speech_end_seconds(segments)

    for segment in segments:
        # The list's element type is nullable, and a null entry carries nothing
        # to aggregate.
        if not isinstance(segment, dict):
            continue
        # A meeting recorded across an upgrade holds both spellings of a channel;
        # they are aggregated as one, under the current name.
        channel = canonical_channel(segment.get("Channel", None))
        if is_speech_channel(channel):
            if segment.get("SentimentWeighted", None):
                LOGGER.debug("Aggregating sentiment entry", extra=segment)
                sentiment_entry: SentimentEntry = {
                    "Id": segment["SegmentId"],
                    "BeginOffsetMillis": segment["StartTime"] * 1000,
                    "EndOffsetMillis": segment["EndTime"] * 1000,
                    "Sentiment": segment["Sentiment"],
                    "Score": segment["SentimentWeighted"],
                }
                if channel in sentiment_entry_list_by_channel:
                    tmp = sentiment_entry_list_by_channel[channel].get("SentimentList", [])
                    tmp.append(sentiment_entry)
                    sentiment_list_obj = {"SentimentList": tmp}
                else:
                    sentiment_list_obj = {"SentimentList": [sentiment_entry]}

                sentiment_entry_list_by_channel[channel] = sentiment_list_obj

    aggregated_sentiment: Sentiment = {}
    overall_sentiment: Dict[ChannelType, float] = {}
    sentiment_by_period_by_channel: Dict[ChannelType, List[SentimentByPeriodEntry]] = {}

    for channel in sentiment_entry_list_by_channel.keys():
        sentiment_list = sentiment_entry_list_by_channel[channel].get("SentimentList", [])
        sentiment_scores = [i["Score"] for i in sentiment_list]
        sentiment_average = fmean(sentiment_scores) if sentiment_scores else 0

        sentiment_per_quarter = get_sentiment_per_quarter(sentiment_list) if sentiment_list else []

        overall_sentiment[channel] = sentiment_average
        sentiment_by_period_by_channel[channel] = sentiment_per_quarter

    LOGGER.debug("Overall Sentiment: ", extra=dict(DebugOverallSentiment=overall_sentiment))
    LOGGER.debug(
        "Sentiment by Period: ", extra=dict(DebugSentimentByPeriod=sentiment_by_period_by_channel)
    )

    aggregated_sentiment = {
        "OverallSentiment": overall_sentiment,
        "SentimentByPeriod": {"QUARTER": sentiment_by_period_by_channel},
    }

    return aggregated_sentiment, latest_end


async def get_aggregate_call_data(
    message: Dict[str, object],
    appsync_session: AppsyncAsyncClientSession,
    record_latest_end: Optional[float] = None,
) -> Optional[Dict]:
    """The meeting aggregation to write, or None when there is nothing to write.

    ``record_latest_end`` is the latest end time of the final speech segments
    the current Kinesis record carries. Those segments are written after this
    read, so the read may not include them. Without it, ``message`` is taken as
    the record's only segment.
    """
    call_id = message.get("CallId")
    if not call_id:
        error_message = "callid does not exist"
        raise TypeError(error_message)

    sentiment, latest_end = await get_sentiment_and_latest_end(
        message=message, appsync_session=appsync_session
    )
    if sentiment is None:
        # The scored segments could not be read. Writing the aggregation anyway
        # would replace the stored one with an empty aggregation.
        return None

    # The meeting's duration so far: the latest end of all its final speech
    # segments, which the read above returned, or of this record's own final
    # speech segments when one of them ends later still (they may not be
    # readable yet). Both are limited to the speech channels.
    if record_latest_end is None:
        record_latest_end = latest_speech_end_seconds([message])
    total_duration = max(record_latest_end, latest_end) * 1000

    updated_at = message.get("UpdatedAt", datetime.utcnow().astimezone().isoformat())
    event_type = message.get("EventType", "")
    if event_type == "END":
        call_aggregation: Dict[str, object] = {"CallId": call_id, "Sentiment": sentiment}
    else:
        call_aggregation: Dict[str, object] = {
            "CallId": call_id,
            "TotalConversationDurationMillis": total_duration,
            "Sentiment": sentiment,
            "UpdatedAt": updated_at,
        }

    return call_aggregation


async def get_call_aggregation_tasks(
    message: Dict[str, object],
    appsync_session: AppsyncAsyncClientSession,
    record_latest_end: Optional[float] = None,
) -> List[Coroutine]:
    """The updateCallAggregation task for this message, or none to send.

    ``record_latest_end`` is passed through to ``get_aggregate_call_data``.
    """
    call_aggregation = await get_aggregate_call_data(
        message=message, appsync_session=appsync_session, record_latest_end=record_latest_end
    )
    if call_aggregation is None:
        return []

    if not appsync_session.client.schema:
        raise ValueError("invalid AppSync schema")
    schema = DSLSchema(appsync_session.client.schema)

    query = dsl_gql(
        DSLMutation(
            schema.Mutation.updateCallAggregation.args(input=call_aggregation).select(
                *call_fields(schema)
            )
        )
    )

    tasks = []

    tasks.append(
        execute_gql_query_with_retries(
            query,
            client_session=appsync_session,
            logger=LOGGER,
            should_ignore_exception_fn=is_put_condition_failure,
        ),
    )

    return tasks


async def execute_update_call_aggregation_mutation(
    message: Dict[str, object],
    appsync_session: AppsyncAsyncClientSession,
) -> Dict:
    call_aggregation = await get_aggregate_call_data(
        message=message, appsync_session=appsync_session
    )
    if call_aggregation is None:
        # Same reasoning as get_call_aggregation_tasks: nothing to write beats
        # replacing the stored aggregation with an empty one.
        return {"ok": True}

    if not appsync_session.client.schema:
        raise ValueError("invalid AppSync schema")
    schema = DSLSchema(appsync_session.client.schema)

    query = dsl_gql(
        DSLMutation(
            schema.Mutation.updateCallAggregation.args(input=call_aggregation).select(
                *call_fields(schema)
            )
        )
    )

    result = await execute_gql_query_with_retries(
        query,
        client_session=appsync_session,
        logger=LOGGER,
        should_ignore_exception_fn=is_put_condition_failure,
    )

    query_string = print_ast(query)
    LOGGER.debug("transcript aggregation mutation", extra=dict(query=query_string, result=result))
    return result


async def execute_add_s3_recording_mutation(
    message: Dict[str, Any],
    appsync_session: AppsyncAsyncClientSession,
) -> Dict:
    recording_url = message.get("RecordingUrl")
    if not recording_url:
        error_message = "recording url doesn't exist in add s3 recording url event"
        raise TypeError(error_message)

    if not appsync_session.client.schema:
        raise ValueError("invalid AppSync schema")
    schema = DSLSchema(appsync_session.client.schema)

    query = dsl_gql(
        DSLMutation(
            schema.Mutation.updateRecordingUrl.args(
                input={**message, "RecordingUrl": recording_url}
            ).select(*call_fields(schema))
        )
    )

    result = await execute_gql_query_with_retries(
        query,
        client_session=appsync_session,
        logger=LOGGER,
    )

    query_string = print_ast(query)
    LOGGER.debug("query result", extra=dict(query=query_string, result=result))

    return result


async def execute_update_capture_context_mutation(
    message: Dict[str, Any],
    appsync_session: AppsyncAsyncClientSession,
) -> Dict:
    """Sets the capture context fields an UPDATE_CAPTURE_CONTEXT event carries.

    Sent after START when the meeting is transcribed by another engine than the
    one START named. The write is conditional on the meeting existing, so an
    event processed before its START in the same batch fails and is redelivered.
    """
    call_id = message.get("CallId")
    fields = capture_context_fields(message)
    if not call_id or not fields:
        error_message = "update capture context event has no CallId or no field to set"
        raise TypeError(error_message)

    if not appsync_session.client.schema:
        raise ValueError("invalid AppSync schema")
    schema = DSLSchema(appsync_session.client.schema)

    query = dsl_gql(
        DSLMutation(
            schema.Mutation.updateCaptureContext.args(input={"CallId": call_id, **fields}).select(
                schema.Call.CallId,
                schema.Call.CaptureSource,
                schema.Call.TranscriptionEngine,
                schema.Call.TranscriptionLanguage,
                schema.Call.SpeakerSeparation,
            )
        )
    )

    result = await execute_gql_query_with_retries(
        query,
        client_session=appsync_session,
        logger=LOGGER,
    )

    query_string = print_ast(query)
    LOGGER.debug("query result", extra=dict(query=query_string, result=result))

    return result


async def execute_add_s3_video_recording_mutation(
    message: Dict[str, Any],
    appsync_session: AppsyncAsyncClientSession,
) -> Dict:
    video_recording_url = message.get("VideoRecordingUrl")
    if not video_recording_url:
        error_message = "video recording url doesn't exist in add s3 video recording url event"
        raise TypeError(error_message)

    if not appsync_session.client.schema:
        raise ValueError("invalid AppSync schema")
    schema = DSLSchema(appsync_session.client.schema)

    query = dsl_gql(
        DSLMutation(
            schema.Mutation.updateVideoRecordingUrl.args(
                input={**message, "VideoRecordingUrl": video_recording_url}
            ).select(*call_fields(schema))
        )
    )

    result = await execute_gql_query_with_retries(
        query,
        client_session=appsync_session,
        logger=LOGGER,
    )

    query_string = print_ast(query)
    LOGGER.debug("query result", extra=dict(query=query_string, result=result))

    return result


async def execute_add_video_keyframe_mutation(
    message: Dict[str, Any],
    appsync_session: AppsyncAsyncClientSession,
) -> Dict:
    """Store one key frame of the shared screen on the meeting (ADD_VIDEO_KEYFRAME).

    The frame takes the meeting's Owner and SharedWith, read here rather than
    trusted from the event, so it is visible to the people who can see the
    transcript; see keyframe_input.py. A meeting that is not there yet fails the
    record, which is redelivered, as a transcript segment's would be.

    An event that can never be stored -- a field missing or of the wrong kind --
    is logged and dropped rather than raised: raising reports the record for
    redelivery, and it would fail the same way every time while holding up the
    rest of its shard (see the failure policy in batch_item_failures.py).
    """
    try:
        keyframe = build_keyframe_input(
            message,
            owner=None,
            shared_with=None,
            # Key frames are not deleted with the transcript, so they keep the
            # transcript retention period in full.
            expires_after=get_transcription_ttl(limited_by_deletion=False),
        )
    except KeyframeEventError as error:
        LOGGER.error(
            "dropping an ADD_VIDEO_KEYFRAME event that cannot be stored: %s",
            error,
            extra=dict(call_id=message.get("CallId"), frame_id=message.get("FrameId")),
        )
        return {"ok": False, "Dropped": True}

    if not appsync_session.client.schema:
        raise ValueError("invalid AppSync schema")
    schema = DSLSchema(appsync_session.client.schema)

    access_query = dsl_gql(
        DSLQuery(
            schema.Query.getCall.args(CallId=keyframe["CallId"]).select(
                schema.Call.CallId,
                schema.Call.Owner,
                schema.Call.SharedWith,
            )
        )
    )
    access = await execute_gql_query_with_retries(
        access_query,
        client_session=appsync_session,
        logger=LOGGER,
    )
    call = require_graphql_field(access, "getCall", logger=LOGGER)
    for field in ("Owner", "SharedWith"):
        if call.get(field) is not None:
            keyframe[field] = call[field]

    query = dsl_gql(
        DSLMutation(
            schema.Mutation.addVideoKeyframe.args(input=keyframe).select(
                *video_keyframe_fields(schema)
            )
        )
    )
    result = await execute_gql_query_with_retries(
        query,
        client_session=appsync_session,
        logger=LOGGER,
    )
    LOGGER.debug("video keyframe stored", extra=dict(frame_id=keyframe["FrameId"]))
    queue_keyframe_text(keyframe)
    return result


def queue_keyframe_text(keyframe: Mapping[str, Any]) -> None:
    """Have the frame's text read, asynchronously, once it is stored.

    Fire-and-forget, as the agenda pass: nothing in this batch waits on it, and
    a frame without its text is handled by every reader. The extractor skips a
    frame that already has its text, so a redelivered event costs nothing.
    """
    if not KEYFRAME_TEXT_EXTRACTOR_ARN:
        return
    try:
        LAMBDA_HOOK_CLIENT.invoke(
            FunctionName=KEYFRAME_TEXT_EXTRACTOR_ARN,
            InvocationType="Event",
            Payload=json.dumps(
                {
                    "CallId": keyframe["CallId"],
                    "FrameId": keyframe["FrameId"],
                    "ImageUrl": keyframe["ImageUrl"],
                }
            ),
        )
    except Exception:  # pylint: disable=broad-except
        LOGGER.exception(
            "Could not queue the text read of a key frame; continuing",
            extra=dict(call_id=keyframe.get("CallId"), frame_id=keyframe.get("FrameId")),
        )


async def execute_add_call_summary_text_mutation(
    message: Dict[str, Any],
    appsync_session: AppsyncAsyncClientSession,
) -> Dict:
    """Store the end-of-meeting summary, and the outcome of the run beside it.

    A run that produced nothing usable -- the summariser's placeholder text, or
    none -- stores no text. The input field is required, so it is sent blank,
    and updateCall.request.vtl writes no attribute for a blank value: whatever
    the meeting already holds stays, and the state says the run failed, which
    is what the meeting page shows alongside its Regenerate action.
    """
    calltext = message.get("CallSummaryText", None)
    call_summary_text = ""
    if summary_was_produced(calltext):
        call_summary_text = calltext

    if not appsync_session.client.schema:
        raise ValueError("invalid AppSync schema")
    schema = DSLSchema(appsync_session.client.schema)

    query = dsl_gql(
        DSLMutation(
            schema.Mutation.addCallSummaryText.args(
                input={
                    **message,
                    "CallSummaryText": call_summary_text,
                    "CallSummaryState": summary_outcome_state(calltext),
                }
            ).select(*call_fields(schema))
        )
    )

    result = await execute_gql_query_with_retries(
        query,
        client_session=appsync_session,
        logger=LOGGER,
    )

    query_string = print_ast(query)
    LOGGER.debug("query result", extra=dict(query=query_string, result=result))

    return result


async def execute_add_agent_assist_mutation(
    message: Dict[str, Any],
    appsync_session: AppsyncAsyncClientSession,
) -> Dict:
    if not appsync_session.client.schema:
        raise ValueError("invalid AppSync schema")
    schema = DSLSchema(appsync_session.client.schema)

    LOGGER.debug("Add Agent Assist Mutation message: %s", json.dumps(message))
    query = dsl_gql(
        DSLMutation(
            schema.Mutation.addTranscriptSegment.args(input=message).select(
                *transcript_segment_fields(schema),
            )
        )
    )

    LOGGER.debug("Executing QUERY: %s", query)

    result = await execute_gql_query_with_retries(
        query,
        client_session=appsync_session,
        logger=LOGGER,
    )

    query_string = print_ast(query)
    LOGGER.debug("query result", extra=dict(query=query_string, result=result))

    return result


async def execute_update_agent_mutation(
    message: Dict[str, Any],
    appsync_session: AppsyncAsyncClientSession,
) -> Dict:
    agentId = message.get("AgentId")
    if not agentId:
        error_message = "AgentId doesn't exist in UPDATE_AGENT event"
        raise TypeError(error_message)

    if not appsync_session.client.schema:
        raise ValueError("invalid AppSync schema")
    schema = DSLSchema(appsync_session.client.schema)

    query = dsl_gql(
        DSLMutation(
            schema.Mutation.updateAgent.args(input={**message, "AgentId": agentId}).select(
                *call_fields(schema)
            )
        )
    )

    result = await execute_gql_query_with_retries(
        query,
        client_session=appsync_session,
        logger=LOGGER,
    )

    query_string = print_ast(query)
    LOGGER.debug("query result", extra=dict(query=query_string, result=result))

    return result


##########################################################################
# Transcript Lambda Hook
# User provided function should return a copy of the input event with
# optionally modified "Transcript" field (to support custom redaction or
# other transcript manipulation.
# The original transcript can be optionally returned as "OriginalTranscript"
# to be used as input for Agent Assist bot or Lambda, otherwise "Transcript"
# field is used for Agent Assist input.
##########################################################################


def invoke_transcript_lambda_hook(message: Dict[str, Any]):
    if not message.get("IsPartial") or not transcript_hook_nonpartial_only():
        LOGGER.debug("Transcript Lambda Hook Arn: %s", TRANSCRIPT_LAMBDA_HOOK_FUNCTION_ARN)
        LOGGER.debug("Transcript Lambda Hook Request: %s", message)
        lambda_response = LAMBDA_HOOK_CLIENT.invoke(
            FunctionName=TRANSCRIPT_LAMBDA_HOOK_FUNCTION_ARN,
            InvocationType="RequestResponse",
            Payload=json.dumps(message),
        )
        LOGGER.debug("Transcript Lambda Hook Response: ", extra=lambda_response)
        try:
            message = json.loads(lambda_response.get("Payload").read().decode("utf-8"))
        except Exception as error:
            LOGGER.error(
                "Transcript Lambda Hook result payload parsing exception. Lambda must return JSON object with (modified) input event fields",
                extra=error,
            )
        # A hook written for an earlier version may hand back an older channel name.
        if isinstance(message, dict) and message.get("Channel"):
            message["Channel"] = canonical_channel(message["Channel"])
    return message


def queue_agenda_derivation(call_id: str) -> None:
    """Queue the automatic agenda pass for a meeting, fire-and-forget.

    Asynchronous for the reason the end-of-meeting speaker-naming invoke is: a
    synchronous one would hold up every other record in the batch, live segments
    for other meetings included, for the length of a model call. The deriver
    decides for itself whether the pass is still wanted -- the meeting may have
    an agenda, or another environment may already have run it -- so queueing it
    a few times over costs only a conditional write each. A failure to queue is
    logged and the segment processed regardless: an agenda is an enhancement.
    """
    # Bounded: a warm environment sees meeting after meeting. Forgetting them
    # all costs at most a repeat queue each, which the deriver's claim absorbs.
    if len(AGENDA_PASS_QUEUED) >= 5000:
        AGENDA_PASS_QUEUED.clear()
    AGENDA_PASS_QUEUED.add(call_id)
    try:
        LAMBDA_HOOK_CLIENT.invoke(
            FunctionName=AGENDA_DERIVER_ARN,
            InvocationType="Event",
            Payload=json.dumps({"CallId": call_id, "Trigger": "auto"}),
        )
        LOGGER.debug("Queued the automatic agenda pass", extra=dict(call_id=call_id))
    except Exception:  # pylint: disable=broad-except
        LOGGER.exception(
            "Could not queue the automatic agenda pass; continuing",
            extra=dict(call_id=call_id),
        )


async def get_call_details(
    message: Dict[str, Any],
    appsync_session: AppsyncAsyncClientSession,
) -> Dict:
    if not appsync_session.client.schema:
        raise ValueError("invalid AppSync schema")

    global CUSTOMER_PHONE_NUMBER
    global CALL_ID

    schema = DSLSchema(appsync_session.client.schema)

    query = dsl_gql(
        DSLQuery(
            schema.Query.getCall.args(CallId=message["CallId"]).select(
                *call_fields(schema),
            )
        )
    )

    result = await execute_gql_query_with_retries(
        query,
        client_session=appsync_session,
        logger=LOGGER,
    )

    # Required, not optional: every field returned below comes from the meeting
    # record, so a response without one leaves nothing to return. Raising fails
    # the Kinesis record, which the event source mapping redelivers and finally
    # describes to its on-failure destination -- the opposite of returning blanks
    # and reporting success.
    call = require_graphql_field(result, "getCall", logger=LOGGER)
    LOGGER.debug("Get Call result %s", json.dumps(call))

    # CallId is required because it is how the caller names the meeting. The
    # other three the schema declares nullable, so a stored meeting with any of
    # them unset is a response this has to return.
    CALL_ID = require_graphql_field(call, "CallId", logger=LOGGER)
    CUSTOMER_PHONE_NUMBER = call.get("CustomerPhoneNumber")
    call_summary = call.get("CallSummaryText", "")
    people_can_access = call.get("SharedWith", None)

    return dict(
        CustomerPhoneNumber=CUSTOMER_PHONE_NUMBER,
        CallId=CALL_ID,
        CallDataStream=CALL_DATA_STREAM_NAME,
        CallSummaryText=call_summary,
        Owner=call.get("Owner"),
        SharedWith=people_can_access,
    )


##########################################################################
# Fix CamelCasing from Chime
##########################################################################


##########################################################################
# merge dicts
##########################################################################


##########################################################################
# check for agent assist wake phrase
##########################################################################
##########################################################################
# Main event processing
##########################################################################


async def execute_process_event_api_mutation(
    message: Dict[str, Any],
    appsync_session: AppsyncAsyncClientSession,
    agent_assist_args: Dict[str, Any],
    sentiment_analysis_args: Dict[str, Any],
) -> Dict[Literal["successes", "errors"], List]:
    """Executes AppSync API Mutation"""
    # pylint: disable=global-statement
    global IS_LAMBDA_AGENT_ASSIST_ENABLED
    # pylint: enable=global-statement

    IS_LAMBDA_AGENT_ASSIST_ENABLED = agent_assist_args.get("is_lambda_agent_assist_enabled")

    return_value: Dict[Literal["successes", "errors"], List] = {
        "successes": [],
        "errors": [],
    }

    metadata = None

    # normalize the casing
    message = convert_keys_to_uppercamelcase(message)

    metadata_str = message.get("Metadata", None)
    if metadata_str is not None:
        metadata = json.loads(metadata_str)
        metadata = convert_keys_to_uppercamelcase(metadata)

        message = merge_dicts(message, metadata)

    event_type_map = dict(
        STARTED="START",
        START="START",
        COMPLETED="END",
        END="END",
        SEGMENTS="ADD_TRANSCRIPT_SEGMENT",
        ADD_TRANSCRIPT_SEGMENT="ADD_TRANSCRIPT_SEGMENT",
        FAILED="ERRORED",
        UPDATE_AGENT="UPDATE_AGENT",
        ADD_SUMMARY="ADD_SUMMARY",
        ADD_AGENT_ASSIST="ADD_AGENT_ASSIST",
        ADD_S3_RECORDING_URL="ADD_S3_RECORDING_URL",
        ADD_S3_VIDEO_RECORDING_URL="ADD_S3_VIDEO_RECORDING_URL",
        ADD_VIDEO_KEYFRAME="ADD_VIDEO_KEYFRAME",
        UPDATE_CAPTURE_CONTEXT="UPDATE_CAPTURE_CONTEXT",
    )

    msg_event_type = message.get("EventType", "")
    event_type = event_type_map.get(msg_event_type, "")

    if event_type == "":
        # This is possibly a message from Flume. Let's fix the message if it is
        if message.get("TranscriptEvent", "") != "":
            message["EventType"] = "ADD_TRANSCRIPT_SEGMENT"
            event_type = "ADD_TRANSCRIPT_SEGMENT"

    message["EventType"] = event_type
    # default expiration for meeting (note: transcript segments can have a different - earlier - expiration)
    message["ExpiresAfter"] = get_meeting_ttl()

    LOGGER.debug("Process event. eventType: %s, callId: %s", event_type, message.get("CallId", ""))

    if event_type == "START":
        # CREATE CALL
        LOGGER.debug("CREATE CALL")
        response = await execute_create_call_mutation(
            message=message, appsync_session=appsync_session
        )

        if isinstance(response, Exception):
            return_value["errors"].append(response)
        else:
            return_value["successes"].append(response)
            # Counted on the same condition as the start-of-call hook below, and
            # for the same reason: a redelivered START must not read as a second
            # meeting. Kept out of the hook's own branch because the count is
            # wanted whether or not a deployment configures a hook.
            #
            # Inside the success branch, matching the END branch below. A
            # non-duplicate failure raises rather than being returned here, so
            # in practice this line is unreachable with an exception in hand --
            # but the branch above shows the contract allows one, and a meeting
            # that could not be created has not started.
            if not was_already_created(response):
                METRICS.count(metric_names.MEETINGS_STARTED)

        # At most once per meeting. Redelivery is routine on this stream -- the
        # END branch below explains why -- and a deployment's own start-of-call
        # hook would read a second invocation as a second meeting starting.
        #
        # At most, not exactly: a rejected condition is read as "somebody already
        # created this meeting", and it is also what a first delivery sees when
        # its own earlier attempt stored the meeting and then came back as a
        # transport error, or when the invoke below raised after the meeting was
        # created and the record came back. Delivering the hook exactly once
        # needs a durable record of having delivered it, which this does not
        # have; skipping it is the safer of the two mistakes, because invoking it
        # twice is visible in whatever the hook does.
        if START_OF_CALL_LAMBDA_HOOK_FUNCTION_ARN and not was_already_created(response):
            payload = dict(
                CustomerPhoneNumber=CUSTOMER_PHONE_NUMBER,
                CallId=CALL_ID,
                CallDataStream=CALL_DATA_STREAM_NAME,
            )
            LAMBDA_HOOK_CLIENT.invoke(
                FunctionName=START_OF_CALL_LAMBDA_HOOK_FUNCTION_ARN,
                InvocationType="Event",
                Payload=json.dumps(payload),
            )
        elif START_OF_CALL_LAMBDA_HOOK_FUNCTION_ARN:
            LOGGER.info(
                "START event for a meeting that is already created; not invoking the "
                "start-of-call hook again",
                extra=dict(call_id=message.get("CallId")),
            )

    elif event_type in [
        "END",
    ]:
        LOGGER.debug("END Event: update status")
        # Read the status before anything here writes ENDED, so a redelivered END
        # is distinguishable from the first one. The event source mapping reports
        # partial batch failures, which means every record from a failing record
        # onwards is redelivered; the summary orchestrator is not idempotent, so
        # invoking it again would produce a second summary and a second
        # ADD_SUMMARY write for the same meeting.
        was_already_ended = (
            await get_call_status(message=message, appsync_session=appsync_session) == "ENDED"
        )

        # Name the separated voices once at the end of the meeting.
        #
        # Only when summaries are OFF. When they are on, the summary orchestrator
        # runs the naming pass itself, synchronously, immediately before
        # summarising -- which is what gets the names into the summary. Doing it
        # from here as well would pay for a second model call for nothing.
        #
        # Fire-and-forget deliberately. A synchronous invoke here would block this
        # event loop, and with it every other record in the same Kinesis batch
        # including live segments for other meetings; it would also be retried by
        # the client's own retry policy if it outran the socket timeout, running
        # the whole pass several times over. Nothing downstream waits on the
        # names, so there is nothing to order against.
        if (
            SPEAKER_NAME_RESOLVER_ARN
            and not IS_TRANSCRIPT_SUMMARY_ENABLED
            and not was_already_ended
        ):
            try:
                LAMBDA_HOOK_CLIENT.invoke(
                    FunctionName=SPEAKER_NAME_RESOLVER_ARN,
                    InvocationType="Event",
                    Payload=json.dumps({"CallId": message.get("CallId"), "Trigger": "meeting-end"}),
                )
                LOGGER.debug("END Event: queued the speaker name resolver")
            except Exception:  # pylint: disable=broad-except
                LOGGER.exception(
                    "END Event: could not queue speaker naming; continuing to close the meeting",
                    extra=dict(call_id=message.get("CallId")),
                )

        # The transcript deletion, scheduled before the summary is asked for and
        # not wrapped in a try, for the same reason the summary invoke below is
        # not: a failure redelivers the record with the meeting still open, so
        # the retry schedules it. The function is idempotent, and returns at
        # once while the Retention section leaves transcripts alone.
        if TRANSCRIPT_DELETION_FUNCTION_ARN and not was_already_ended:
            LAMBDA_HOOK_CLIENT.invoke(
                FunctionName=TRANSCRIPT_DELETION_FUNCTION_ARN,
                InvocationType="Event",
                Payload=json.dumps({"CallId": message.get("CallId"), "Trigger": "meeting-ended"}),
            )
            LOGGER.debug("END Event: told the transcript deletion function")

        # Summary first, status second. The invoke is not wrapped in a try, so a
        # transient failure propagates and the record is redelivered — and it has
        # to find the meeting still open when it comes back, or the check above
        # would skip the summary on the retry and the meeting would end with no
        # summary at all. Ordering it ahead of the status mutation makes the worst
        # case a duplicate summary rather than a missing one.
        summary_queued = IS_TRANSCRIPT_SUMMARY_ENABLED and not was_already_ended
        if summary_queued:
            LAMBDA_HOOK_CLIENT.invoke(
                FunctionName=ASYNC_TRANSCRIPT_SUMMARY_ORCHESTRATOR_ARN,
                InvocationType="Event",
                Payload=json.dumps(message),
            )
            LOGGER.debug("END Event: Invoked Async Transcript Summary Lambda")
        elif IS_TRANSCRIPT_SUMMARY_ENABLED:
            LOGGER.info(
                "END event for a meeting that is already ENDED; not invoking the "
                "transcript summary again",
                extra=dict(call_id=message.get("CallId")),
            )

        # The status write carries the "summary in progress" state when a
        # summary has just been queued, so the meeting page can tell a summary
        # on its way from no summary at all. Only then: with summaries switched
        # off nothing is coming, and a redelivered END must not reset the state
        # of a summary that may already have landed.
        status_message = (
            {**message, "CallSummaryState": summary_queued_state()} if summary_queued else message
        )
        response = await execute_update_call_status_mutation(
            message=status_message, appsync_session=appsync_session
        )
        if isinstance(response, Exception):
            return_value["errors"].append(response)
        else:
            return_value["successes"].append(response)
            # Counted once per meeting, on the write that closed it, so that a
            # redelivered END does not produce a second ending. A failed write
            # is not counted at all -- it raises rather than reaching here, the
            # batch processor attributes it to this record's sequence number,
            # and the record is redelivered with the meeting still open.
            if not was_already_ended:
                METRICS.count(metric_names.MEETINGS_ENDED)

    elif event_type == "ADD_SUMMARY":
        LOGGER.debug("ADD_SUMMARY MUTATION ")
        response = await execute_add_call_summary_text_mutation(
            message=message, appsync_session=appsync_session
        )

        if isinstance(response, Exception):
            return_value["errors"].append(response)
        else:
            return_value["successes"].append(response)
            # The summary is stored, so the transcript deletion can count its
            # delay from now. Fire-and-forget: if this is lost, the deletion
            # scheduled at the end of the meeting still runs, later. Not for a
            # failed run's placeholder text: the transcript then waits for the
            # end-of-meeting fallback, leaving time to regenerate the summary.
            if TRANSCRIPT_DELETION_FUNCTION_ARN and summary_was_produced(
                message.get("CallSummaryText")
            ):
                try:
                    LAMBDA_HOOK_CLIENT.invoke(
                        FunctionName=TRANSCRIPT_DELETION_FUNCTION_ARN,
                        InvocationType="Event",
                        Payload=json.dumps(
                            {"CallId": message.get("CallId"), "Trigger": "summary-written"}
                        ),
                    )
                except Exception:  # pylint: disable=broad-except
                    LOGGER.exception(
                        "ADD_SUMMARY: could not tell the transcript deletion function; the "
                        "deletion scheduled at the end of the meeting still applies",
                        extra=dict(call_id=message.get("CallId")),
                    )

        if POST_CALL_SUMMARY_LAMBDA_HOOK_FUNCTION_ARN:
            payload = await get_call_details(message=message, appsync_session=appsync_session)

            LAMBDA_HOOK_CLIENT.invoke(
                FunctionName=POST_CALL_SUMMARY_LAMBDA_HOOK_FUNCTION_ARN,
                InvocationType="Event",
                Payload=json.dumps(payload),
            )

    elif event_type == "ADD_AGENT_ASSIST":
        LOGGER.debug("ADD_AGENT_ASSIST MUTATION ")
        normalized_message = normalize_transcript_segments({**message})

        # The answer takes the meeting's Owner and SharedWith, as the meeting's
        # other transcript segments do, rather than the identity of whoever asked.
        meeting = await get_call_details(
            message=normalized_message[0], appsync_session=appsync_session
        )
        apply_meeting_access(normalized_message[0], meeting)

        response = await execute_add_agent_assist_mutation(
            message=normalized_message[0], appsync_session=appsync_session
        )

        if isinstance(response, Exception):
            return_value["errors"].append(response)
        else:
            return_value["successes"].append(response)

    elif event_type == "ADD_TRANSCRIPT_SEGMENT":
        # ADD_TRANSCRIPT_SEGMENT event supports these types of message structure.
        #   The logic for populating transcripts, sentiment values and agent assist messages depend on
        #    which one of these json structures are populated in the KDS Event message.
        #
        #  1. custom i.e. source populates invidividual transcript fields, including optional sentiment field
        #  2. TranscriptEvent - json structure from standard Transcribe API

        # normalize_transcript_segments also sets transcript segment expiration time
        normalized_messages = normalize_transcript_segments({**message})

        add_transcript_tasks = []
        add_transcript_sentiment_tasks = []

        for normalized_message in normalized_messages:
            # Invoke custom lambda hook (if any) and use returned version of message.
            if TRANSCRIPT_LAMBDA_HOOK_FUNCTION_ARN:
                normalized_message = invoke_transcript_lambda_hook(normalized_message)

            payload = await get_call_details(
                message=normalized_message, appsync_session=appsync_session
            )

            # The Owner the event itself carried, before the segment takes the
            # meeting's: the agent assist request below is made as before, and
            # the orchestrator passes its Owner on as the identity of whoever
            # asked, which a wake phrase spoken in the meeting does not have.
            event_owner = normalized_message.get("Owner")

            LOGGER.debug("Add Transcript Segment")
            add_transcript_tasks.extend(
                add_transcript_segments(
                    message=normalized_message,
                    meeting=payload,
                    appsync_session=appsync_session,
                )
            )
            # Sentiment and the wake phrase are for what was said: a message
            # typed in the meeting chat is stored, and the transcript hook above
            # sees it, but it gets neither.
            analyses = segment_analyses(
                is_speech=is_speech_channel(normalized_message.get("Channel")),
                is_partial=normalized_message["IsPartial"],
                sentiment_enabled=IS_SENTIMENT_ANALYSIS_ENABLED,
                agent_assist_enabled=IS_LAMBDA_AGENT_ASSIST_ENABLED,
                wake_pattern=wake_phrase_pattern,
                transcript=normalized_message.get("Transcript", ""),
            )
            if analyses.sentiment:
                LOGGER.debug("Add Sentiment Analysis")
                add_transcript_sentiment_tasks.extend(
                    add_transcript_sentiment_analysis(
                        message=normalized_message,
                        sentiment_analysis_args=sentiment_analysis_args,
                        appsync_session=appsync_session,
                    )
                )
            if (
                AGENDA_DERIVER_ARN
                and not normalized_message["IsPartial"]
                # A meeting already queued here needs no read of the threshold.
                and normalized_message.get("CallId") not in AGENDA_PASS_QUEUED
                and agenda_derivation_due(
                    normalized_message.get("CallId"),
                    normalized_message.get("EndTime"),
                    agenda_derivation_threshold(),
                    AGENDA_PASS_QUEUED,
                )
            ):
                queue_agenda_derivation(normalized_message["CallId"])
            if analyses.agent_assist:
                LAMBDA_HOOK_CLIENT.invoke(
                    FunctionName=ASYNC_AGENT_ASSIST_ORCHESTRATOR_ARN,
                    InvocationType="Event",
                    Payload=json.dumps({**normalized_message, "Owner": event_owner}),
                )

        # One aggregation per record, built for its last final segment. A record
        # can normalize into several final segments, and the last one need not
        # end latest, so the duration is given the latest end of all of them.
        update_call_aggregation_tasks = []
        final_messages = [m for m in normalized_messages if not m["IsPartial"]]
        if final_messages:
            update_call_aggregation_tasks = await get_call_aggregation_tasks(
                message=final_messages[-1],
                appsync_session=appsync_session,
                record_latest_end=latest_speech_end_seconds(final_messages),
            )

        task_responses = await asyncio.gather(
            *add_transcript_tasks,
            *add_transcript_sentiment_tasks,
            *update_call_aggregation_tasks,
            return_exceptions=True,
        )

        # Counted here because this is where a lost transcript line stops being
        # visible to anything else. `return_exceptions=True` turns a failed
        # mutation into an ordinary result, which is collected into
        # return_value["errors"] below and never raised -- so the batch
        # processor does not attribute the record, nothing redelivers it on its
        # own account, and AWS/Lambda Errors stays flat. (Another record in the
        # same batch raising would redeliver this one along with the rest of the
        # tail, but nothing makes that happen for this failure's sake.) These
        # two numbers are the only notice that a segment did not reach the
        # meeting page.
        #
        # gather preserves the order it was given, so the transcript-segment
        # results are exactly the leading slice. Sentiment and aggregation
        # results follow and are deliberately not counted here:
        # those failures cost an annotation, not a transcript line.
        segment_responses = task_responses[: len(add_transcript_tasks)]
        failed_segments = sum(1 for r in segment_responses if isinstance(r, Exception))
        if failed_segments:
            METRICS.count(metric_names.TRANSCRIPT_SEGMENT_WRITE_FAILED, failed_segments)
        if len(segment_responses) - failed_segments:
            METRICS.count(
                metric_names.TRANSCRIPT_SEGMENTS_WRITTEN,
                len(segment_responses) - failed_segments,
            )

        for response in task_responses:
            if isinstance(response, Exception):
                return_value["errors"].append(response)
            else:
                return_value["successes"].append(response)

    elif event_type == "ADD_S3_RECORDING_URL":
        # ADD S3 RECORDING URL
        LOGGER.debug("Add recording url")
        response = await execute_add_s3_recording_mutation(
            message=message, appsync_session=appsync_session
        )
        if isinstance(response, Exception):
            return_value["errors"].append(response)
        else:
            return_value["successes"].append(response)

    elif event_type == "ADD_S3_VIDEO_RECORDING_URL":
        # ADD S3 VIDEO RECORDING URL
        LOGGER.debug("Add video recording url")
        response = await execute_add_s3_video_recording_mutation(
            message=message, appsync_session=appsync_session
        )
        if isinstance(response, Exception):
            return_value["errors"].append(response)
        else:
            return_value["successes"].append(response)

    elif event_type == "UPDATE_CAPTURE_CONTEXT":
        LOGGER.debug("Update capture context")
        response = await execute_update_capture_context_mutation(
            message=message, appsync_session=appsync_session
        )
        if isinstance(response, Exception):
            return_value["errors"].append(response)
        else:
            return_value["successes"].append(response)

    elif event_type == "ADD_VIDEO_KEYFRAME":
        LOGGER.debug("Add video keyframe")
        response = await execute_add_video_keyframe_mutation(
            message=message, appsync_session=appsync_session
        )
        if isinstance(response, Exception):
            return_value["errors"].append(response)
        else:
            return_value["successes"].append(response)

    elif event_type == "UPDATE_AGENT":
        # UPDATE AGENT
        LOGGER.debug("Update AgentId for call")
        response = await execute_update_agent_mutation(
            message=message, appsync_session=appsync_session
        )
        if isinstance(response, Exception):
            return_value["errors"].append(response)
        else:
            return_value["successes"].append(response)
    else:
        LOGGER.warning("unknown event type [%s]", event_type)

    return return_value
