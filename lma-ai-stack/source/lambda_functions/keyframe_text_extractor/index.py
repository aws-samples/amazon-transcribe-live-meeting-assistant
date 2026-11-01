# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Read the text of a key frame of the shared screen, once, and store it with the frame.

Invoked asynchronously by the call event processor for each key frame it stores
(docs/meeting-video.md). The frame's large image is read from the recordings
bucket and given to a vision model (KEYFRAME_TEXT_MODEL_ID), which transcribes
the readable text and notes any chart or picture in a line; the result is
stored on the frame as ``SlideText``. The meeting assistant reads it before it
looks at a picture, the meeting page searches it, and the meeting's document in
the meeting-history knowledge base includes it.

A vision model rather than Amazon Textract: on the measured frames both read
every word, and the model also says what a chart shows, for a tenth of the
price per frame (docs/meeting-video.md, "Slide text").

Idempotent: a frame that already has its text is left alone, so a redelivered
event costs nothing. Every failure is logged and ends the invocation without
an error, and the function's asynchronous invoke allows no retries, so a model
call is never repeated; the frame then simply has no text, which the readers
handle.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict

import boto3
import pure
from bedrock_compat import build_inference_config, extract_text
from botocore.config import Config
from botocore.exceptions import ClientError

# pylint: disable=import-error
from lma_metering import MeteringWriter, bedrock_service, bedrock_usage  # configuration layer

# pylint: enable=import-error

# Standard logging rather than Powertools, as in the agenda deriver: the layer
# that carries Powertools is built for arm64 only.
LOGGER = logging.getLogger()
LOGGER.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

EVENT_SOURCING_TABLE = os.environ.get("EVENT_SOURCING_TABLE", "")
RECORDINGS_BUCKET = os.environ.get("RECORDINGS_BUCKET", "")
KEYFRAMES_PREFIX = os.environ.get("KEYFRAMES_PREFIX", "lma-video-keyframes/")
MODEL_ID = os.environ.get("KEYFRAME_TEXT_MODEL_ID", "amazon.nova-lite-v1:0")
REGION = os.environ.get("AWS_REGION", "us-east-1")

#: Room for a dense page of text and a line about a chart.
MAX_OUTPUT_TOKENS = 2000
#: Largest image passed to the model (Bedrock accepts up to 3.75 MB).
MAX_IMAGE_BYTES = 3_500_000

CLIENT_CONFIG = Config(retries={"mode": "adaptive", "max_attempts": 3})
DYNAMODB = boto3.client("dynamodb", config=CLIENT_CONFIG)
S3 = boto3.client("s3", config=CLIENT_CONFIG)
# As the agenda deriver and the summarizer: inference profiles throttle in bursts.
BEDROCK = boto3.client(
    "bedrock-runtime",
    region_name=REGION,
    config=Config(retries={"mode": "adaptive", "max_attempts": 50}),
)
METERING = MeteringWriter(source="keyframe-text-extractor")


def frame_key(call_id: str, frame_id: str) -> Dict[str, Any]:
    return {"PK": {"S": f"kf#{call_id}"}, "SK": {"S": f"f#{frame_id}"}}


def read_text(image: bytes, call_id: str) -> str:
    """The frame's text, from the model, metered against the meeting."""
    response = BEDROCK.converse(
        modelId=MODEL_ID,
        messages=[
            {
                "role": "user",
                "content": [
                    {"image": {"format": "jpeg", "source": {"bytes": image}}},
                    {"text": pure.PROMPT},
                ],
            }
        ],
        inferenceConfig=build_inference_config(MODEL_ID, MAX_OUTPUT_TOKENS),
    )
    METERING.record(
        call_id, "SlideText", bedrock_service(MODEL_ID), bedrock_usage(response.get("usage"))
    )
    return pure.clean_text(extract_text(response))


def handler(event: Dict[str, Any], _context: Any) -> Dict[str, Any]:
    request = pure.request_from(event or {})
    if request is None:
        LOGGER.warning(
            "Not reading a key frame: the request has no usable CallId, FrameId and ImageUrl"
        )
        return {"status": "skipped", "reason": "bad request"}
    call_id, frame_id = request["CallId"], request["FrameId"]
    key = pure.object_key(request["ImageUrl"], call_id, RECORDINGS_BUCKET, KEYFRAMES_PREFIX)
    if key is None:
        LOGGER.warning(
            "Not reading key frame %s: its image is not in the meeting's folder", frame_id
        )
        return {"status": "skipped", "reason": "image outside the meeting's folder"}

    try:
        stored = DYNAMODB.get_item(
            TableName=EVENT_SOURCING_TABLE,
            Key=frame_key(call_id, frame_id),
            ProjectionExpression="SlideText",
        ).get("Item")
    except ClientError as error:
        LOGGER.error("Could not read key frame %s: %s", frame_id, error.response["Error"]["Code"])
        return {"status": "failed", "reason": "frame unreadable"}
    if stored is None:
        LOGGER.info(
            "Key frame %s is not stored (deleted, or not yet written); nothing to do", frame_id
        )
        return {"status": "skipped", "reason": "no frame"}
    if "SlideText" in stored:
        return {"status": "skipped", "reason": "already read"}

    try:
        body = S3.get_object(Bucket=RECORDINGS_BUCKET, Key=key)["Body"].read(MAX_IMAGE_BYTES + 1)
    except ClientError as error:
        LOGGER.error(
            "Could not read the image of key frame %s: %s",
            frame_id,
            error.response["Error"]["Code"],
        )
        return {"status": "failed", "reason": "image unreadable"}
    if len(body) > MAX_IMAGE_BYTES:
        LOGGER.warning("Key frame %s image is over %d bytes; not read", frame_id, MAX_IMAGE_BYTES)
        return {"status": "skipped", "reason": "image too large"}

    try:
        text = read_text(body, call_id)
    except ClientError as error:
        LOGGER.error(
            "The model could not read key frame %s: %s", frame_id, error.response["Error"]["Code"]
        )
        return {"status": "failed", "reason": "model error"}
    except ValueError as error:
        # extract_text: an answer with no text in it (a reasoning model that
        # spent its whole budget, for one).
        LOGGER.error("The model gave no text for key frame %s: %s", frame_id, error)
        return {"status": "failed", "reason": "no answer"}

    try:
        DYNAMODB.update_item(
            TableName=EVENT_SOURCING_TABLE,
            Key=frame_key(call_id, frame_id),
            UpdateExpression="SET SlideText = :t, SlideTextModel = :m, UpdatedAt = :u",
            # Not onto a frame deleted with its meeting meanwhile.
            ConditionExpression="attribute_exists(PK)",
            ExpressionAttributeValues={
                ":t": {"S": text},
                ":m": {"S": MODEL_ID},
                ":u": {"S": datetime.now(timezone.utc).isoformat()},
            },
        )
    except ClientError as error:
        if error.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return {"status": "skipped", "reason": "frame deleted"}
        LOGGER.error(
            "Could not store the text of key frame %s: %s",
            frame_id,
            error.response["Error"]["Code"],
        )
        return {"status": "failed", "reason": "not stored"}
    LOGGER.info("Read key frame %s of %s: %d characters", frame_id, call_id, len(text))
    return {"status": "read", "characters": len(text)}
