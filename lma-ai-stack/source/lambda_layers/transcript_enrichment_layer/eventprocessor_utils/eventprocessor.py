# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

from typing import TYPE_CHECKING, Any, Coroutine, Dict, List, Literal, Optional
from datetime import datetime, timedelta
from os import getenv
import uuid
import asyncio
from sentiment import ComprehendWeightedSentiment
import jwt

if TYPE_CHECKING:
    from mypy_boto3_comprehend.type_defs import DetectSentimentResponseTypeDef
    from mypy_boto3_comprehend.client import ComprehendClient
else:
    ComprehendClient = object
    DetectSentimentResponseTypeDef = object

MEETING_RECORD_EXPIRATION_IN_DAYS = getenv(
    "MEETING_RECORD_EXPIRATION_IN_DAYS", "90")
TRANSCRIPTION_RECORD_EXPIRATION_IN_DAYS = getenv(
    "TRANSCRIPTION_RECORD_EXPIRATION_IN_DAYS", "90")

SENTIMENT_WEIGHT = dict(POSITIVE=5, NEGATIVE=-5, NEUTRAL=0, MIXED=0)

SENTIMENT_SCORE = dict(
    Positive=0,
    Negative=0,
    Neutral=0,
    Mixed=0,
)

# XXX workaround - this should be moved to the Tumbling Window state
# Contact Lens sends individual Utterances (partials)
# This map is used to concatenate the invididual Utterances
UTTERANCES_MAP: Dict[str, str] = {}

# Get value for DynamboDB TTL field


def get_ttl(expire_in_days):
    return int((datetime.utcnow() + timedelta(days=float(expire_in_days))).timestamp())


def get_meeting_ttl():
    return get_ttl(MEETING_RECORD_EXPIRATION_IN_DAYS)


def get_transcription_ttl():
    return get_ttl(TRANSCRIPTION_RECORD_EXPIRATION_IN_DAYS)

def get_owner_from_jwt(jwt_token):
    decoded_jwt = jwt.decode(jwt_token, options={"verify_signature": False})
    print("DECODED JWT", decoded_jwt)
    return decoded_jwt['username']
    

def transform_segment_to_categories_agent_assist(
    category: str,
    category_details: Dict[str, Any],
    call_id: str,
) -> Dict[str, Any]:
    """Transforms Contact Lens Categories segment payload to Agent Assist"""
    created_at = datetime.utcnow().astimezone().isoformat()
    is_partial = False
    segment_id = str(uuid.uuid4())
    channel = "AGENT_ASSISTANT"

    transcript = f"{category}"
    # get the min and maximum offsets to put a time range
    segment_item = {}
    segment_item["BeginOffsetMillis"] = min(
        (
            point_of_interest["BeginOffsetMillis"]
            for point_of_interest in category_details["PointsOfInterest"]
        )
    )
    segment_item["EndOffsetMillis"] = max(
        (
            point_of_interest["EndOffsetMillis"]
            for point_of_interest in category_details["PointsOfInterest"]
        )
    )

    start_time: float = segment_item["BeginOffsetMillis"] / 1000
    end_time: float = segment_item["EndOffsetMillis"] / 1000

    return dict(
        CallId=call_id,
        Channel=channel,
        CreatedAt=created_at,
        ExpiresAfter=get_transcription_ttl(),
        EndTime=end_time,
        IsPartial=is_partial,
        SegmentId=segment_id,
        StartTime=start_time,
        Status="TRANSCRIBING",
        Transcript=transcript,
    )


def transform_segment_to_issues_agent_assist(
    segment: Dict[str, Any],
    issue: Dict[str, Any],
) -> Dict[str, Any]:
    """Transforms Contact Lens Transcript Issues payload to Agent Assist"""
    # pylint: disable=too-many-locals
    call_id: str = segment["CallId"]
    created_at = datetime.utcnow().astimezone().isoformat()
    is_partial = False
    segment_id = str(uuid.uuid4())
    channel = "AGENT_ASSISTANT"
    segment_item = segment["Transcript"]
    transcript = segment_item["Content"]

    issues_detected = segment.get("Transcript", {}).get("IssuesDetected", [])
    if not issues_detected:
        raise ValueError("Invalid issue segment")

    begin_offset = issue["CharacterOffsets"]["BeginOffsetChar"]
    end_offset = issue["CharacterOffsets"]["EndOffsetChar"]
    issue_transcript = transcript[begin_offset:end_offset]
    start_time: float = segment_item["BeginOffsetMillis"] / 1000
    end_time: float = segment_item["EndOffsetMillis"] / 1000
    end_time = end_time + 0.001  # UI sort order

    return dict(
        CallId=call_id,
        Channel=channel,
        CreatedAt=created_at,
        ExpiresAfter=get_transcription_ttl(),
        EndTime=end_time,
        IsPartial=is_partial,
        SegmentId=segment_id,
        StartTime=start_time,
        Status="TRANSCRIBING",
        Transcript=issue_transcript,
    )


def transform_contact_lens_segment(segment: Dict) -> Dict[str, object]:
    """Transforms Kinesis Stream Transcript Payload to addTranscript API"""
    call_id: str = segment["CallId"]
    contact_id: str = segment["CallId"]
    is_partial: bool
    segment_item: Dict[str, Any]
    segment_id: str
    transcript: str
    utterance: Dict[str, Any] = segment.get("Utterance", None)
    categories: Dict[str, Any] = segment.get("Categories", None)
    contact_lens_transcript: Dict[str, Any] = segment.get("Transcript", None)
    sentiment_args = {}

    # partial transcript
    if "Utterance" in segment:
        is_partial = True
        segment_item = segment["Utterance"]
        segment_id = segment_item["TranscriptId"]
        content = segment_item["PartialContent"]
        UTTERANCES_MAP[segment_id] = UTTERANCES_MAP.get(
            segment_id, "") + " " + content
        transcript = UTTERANCES_MAP[segment_id]
    # final transcript
    elif "Transcript" in segment:
        is_partial = False
        segment_item = segment["Transcript"]
        segment_id = segment_item["Id"]
        transcript = segment_item["Content"]
        # delete utterance concatenatin from global map
        if segment_id in UTTERANCES_MAP:
            del UTTERANCES_MAP[segment_id]
        if "Sentiment" in segment_item:
            sentiment = segment_item.get("Sentiment", "NEUTRAL")
            sentiment_args = dict(
                Sentiment=sentiment,
                SentimentScore=SENTIMENT_SCORE,
                SentimentWeighted=SENTIMENT_WEIGHT.get(sentiment, 0),
            )
    else:
        raise ValueError("Invalid segment type")

    channel = segment_item.get("ParticipantRole", "AGENT")
    # contact lens uses "CUSTOMER" and LCA expects "CALLER"
    if channel == "CUSTOMER":
        channel = "CALLER"
    created_at = datetime.utcnow().astimezone().isoformat()
    # Contact Lens times are in Milliseconds
    # Changing to seconds to normalize units used by the transcript state manager which uses
    # seconds per the Transcribe streaming API
    start_time: float = segment_item["BeginOffsetMillis"] / 1000
    end_time: float = segment_item["EndOffsetMillis"] / 1000

    transcript_segment = dict(
        CallId=call_id,
        ContactId=contact_id,
        Channel=channel,
        CreatedAt=created_at,
        ExpiresAfter=get_transcription_ttl(),
        EndTime=end_time,
        IsPartial=is_partial,
        SegmentId=segment_id,
        StartTime=start_time,
        Status="TRANSCRIBING",
        Transcript=transcript,
        OriginalTranscript=transcript,
        **sentiment_args,
    )

    if (utterance):
        transcript_segment["Utterance"] = utterance

    if (categories):
        transcript_segment["Categories"] = categories

    if (contact_lens_transcript):
        transcript_segment["ContactLensTranscript"] = contact_lens_transcript

    return transcript_segment


# Transform Transcript segment fields
def normalize_transcript_segments(message: Dict) -> List[Dict]:
    """Transforms Kinesis Stream Transcript Payload to addTranscript API"""

    call_id: str = None
    channel: str = None
    speaker: str = None
    segment_id: str = None
    start_time: float = None
    end_time: float = None
    transcript: str = None
    is_partial: bool = None
    sentiment: str = None
    issuesdetected = None
    sentimentWeighted = None
    sentimentScore = None
    status: str = "TRANSCRIBING"
    expires_afer = get_transcription_ttl()
    created_at = datetime.utcnow().astimezone().isoformat()
    owner: str = None
    segments = []

    utteranceEvent = message.get("UtteranceEvent", None)
    transcriptEvent = message.get("TranscriptEvent", None)
    contactLensEvent = message.get("ContactId", None)

    if (utteranceEvent):  # TCA streaming event in KDS
        call_id = message["CallId"]
        channel = utteranceEvent["ParticipantRole"]
        if channel == "CUSTOMER":
            channel = "CALLER"
        segment_id = utteranceEvent["UtteranceId"]
        start_time = utteranceEvent["BeginOffsetMillis"]/1000
        end_time = utteranceEvent["EndOffsetMillis"]/1000
        transcript = utteranceEvent["Transcript"]
        is_partial = utteranceEvent["IsPartial"]
        if not is_partial and utteranceEvent.get("Sentiment", None):
            sentiment: str = utteranceEvent.get("Sentiment", None)
        if not is_partial and utteranceEvent.get("SentimentWeighted", None):
            sentimentWeighted = utteranceEvent.get("SentimentWeighted", None)
        if not is_partial and utteranceEvent.get("SentimentScore", None):
            sentimentScore = utteranceEvent.get("SentimentScore", None)

        if not is_partial and utteranceEvent.get("IssuesDetected", []):
            issuesdetected = utteranceEvent.get("IssuesDetected")
        segments.append(
            dict(
                CallId=call_id,
                Channel=channel,
                SegmentId=segment_id,
                StartTime=start_time,
                EndTime=end_time,
                Transcript=transcript,
                OriginalTranscript=transcript,
                IsPartial=is_partial,
                Sentiment=sentiment,
                SentimentWeighted=sentimentWeighted,
                SentimentScore=sentimentScore,
                IssuesDetected=issuesdetected,
                Status=status,
                ExpiresAfter=expires_afer,
                CreatedAt=created_at,
            )
        )
    elif (transcriptEvent):  # Standard Transcribe streaming event in KDS
        call_id = message["CallId"]
        channel = transcriptEvent["Channel"]
        if channel == "CUSTOMER":
            channel = "CALLER"
        segment_id = transcriptEvent["ResultId"]
        start_time = transcriptEvent["StartTime"]
        end_time = transcriptEvent["EndTime"]
        speaker = transcriptEvent.get("Speaker", None)
        if (not speaker):
            speaker = "Other Participant"
        transcript = transcriptEvent["Transcript"]
        is_partial = transcriptEvent["IsPartial"]
        sentiment = None
        issuesdetected = None
        segments.append(
            dict(
                CallId=call_id,
                Channel=channel,
                SegmentId=segment_id,
                StartTime=start_time,
                EndTime=end_time,
                Speaker=speaker,
                Transcript=transcript,
                OriginalTranscript=transcript,
                IsPartial=is_partial,
                Sentiment=sentiment,
                IssuesDetected=issuesdetected,
                Status=status,
                ExpiresAfter=expires_afer,
                CreatedAt=created_at,
            )
        )
    elif (contactLensEvent):  # Contact Lens event
        call_id = message["ContactId"]
        for segment in message.get("Segments", []):
            # only handle utterances and transcripts - delegate categories to agent assist
            if "Utterance" not in segment and "Transcript" not in segment:
                continue
            transcript_segment = {
                **transform_contact_lens_segment({**segment, "CallId": call_id}),
            }
            segments.append(transcript_segment)

    else:    # custom event message in KDS
        call_id = message["CallId"]
        channel = "CALLER"
        if message.get("Channel", None):
            channel = message["Channel"]
        else:
            iscaller: bool = message.get("IsCaller", True)
            if iscaller:
                channel = "CALLER"
            else:
                channel = "AGENT"

        if message.get("SegmentId", None):
            segment_id = message["SegmentId"]
        else:
            segment_id = str(uuid.uuid4())

        if message.get("BeginOffsetMillis", None):
            start_time = message["BeginOffsetMillis"]
        if message.get("StartTime", None):
            start_time = message["StartTime"]

        if message.get("EndOffsetMillis", None):
            end_time = message["EndOffsetMillis"]
        if message.get("EndTime", None):
            end_time = message["EndTime"]

        speaker = message.get("Speaker", None)
        if (not speaker):
            speaker = "Other Participant"

        transcript = message["Transcript"]
        is_partial = message["IsPartial"]

        if message.get("Sentiment", None):
            sentiment = message["Sentiment"]
        
        if message.get("AccessToken", None):
            owner = get_owner_from_jwt(message.get("AccessToken"))
            
        segments.append(
            dict(
                CallId=call_id,
                Channel=channel,
                SegmentId=segment_id,
                StartTime=start_time,
                EndTime=end_time,
                Transcript=transcript,
                Speaker=speaker,
                OriginalTranscript=transcript,
                IsPartial=is_partial,
                Sentiment=sentiment,
                IssuesDetected=issuesdetected,
                Status=status,
                ExpiresAfter=expires_afer,
                CreatedAt=created_at,
                Owner=owner,
                IdToken=message.get("IdToken", None),
                AccessToken=message.get("AccessToken", None),
                RefreshToken=message.get("RefreshToken", None)
            )
        )

    return segments


async def detect_sentiment(text: str, COMPREHEND_CLIENT: ComprehendClient, COMPREHEND_LANGUAGE_CODE) -> DetectSentimentResponseTypeDef:
    loop = asyncio.get_running_loop()
    sentiment_future = loop.run_in_executor(
        None,
        lambda: COMPREHEND_CLIENT.detect_sentiment(
            Text=text,
            LanguageCode=COMPREHEND_LANGUAGE_CODE,
        ),
    )
    results = await asyncio.gather(sentiment_future)
    result = results[0]
    return result


async def transform_segment_to_add_sentiment(message: Dict, sentiment_analysis_args: Dict) -> Dict[str, object]:

    sentiment_label_in_message = message.get("Sentiment", None)

    sentiment = {}
    # we received sentiment label in transcript, use it.
    if (sentiment_label_in_message):
        sentiment_weighted_in_message = message.get("SentimentWeighted", None)
        sentiment_score_in_message = message.get("SentimentScore", None)

        sentimentlabel: str = ""
        if sentiment_label_in_message.strip() == "":
            sentimentlabel = "NEUTRAL"
        else:
            sentimentlabel = sentiment_label_in_message

        sentiment = dict(
            Sentiment=sentimentlabel,
            SentimentScore=SENTIMENT_SCORE,
            SentimentWeighted=None,
        )

        if (sentiment_weighted_in_message):
            sentiment["SentimentWeighted"] = sentiment_weighted_in_message
        elif sentimentlabel in ["POSITIVE", "NEGATIVE", "NEUTRAL"]:
            sentiment["SentimentWeighted"] = SENTIMENT_WEIGHT.get(
                sentimentlabel, 0)
        if (sentiment_score_in_message):
            sentiment["SentimentScore"] = sentiment_score_in_message

    else:  # did not receive sentiment label, so call Comprehend to figure out sentiment

        text = message.get("OriginalTranscript", message.get("Transcript", ""))
        comprehend_client: ComprehendClient = sentiment_analysis_args.get(
            "comprehend_client")
        comprehend_language_code = sentiment_analysis_args.get(
            "comprehend_language_code", "en")

        sentiment_response: DetectSentimentResponseTypeDef = await detect_sentiment(text, comprehend_client, comprehend_language_code)
        comprehend_weighted_sentiment = ComprehendWeightedSentiment()

        sentiment = {
            k: v for k, v in sentiment_response.items() if k in ["Sentiment", "SentimentScore"]
        }

        if sentiment:
            if sentiment.get("Sentiment") in ["POSITIVE", "NEGATIVE"]:
                sentiment["SentimentWeighted"] = comprehend_weighted_sentiment.get_weighted_sentiment_score(
                    sentiment_response=sentiment_response
                )
    transcript_segment_with_sentiment = {
        **message,
        **sentiment
    }
    return transcript_segment_with_sentiment
