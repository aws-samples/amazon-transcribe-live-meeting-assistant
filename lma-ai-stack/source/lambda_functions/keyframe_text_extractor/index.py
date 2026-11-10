# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Read the text of a key frame of the shared screen, once, and store it with the frame.

Invoked asynchronously by the call event processor for each key frame it stores
(docs/meeting-video.md). The frame's large image is read from the recordings
bucket and given to a vision model (the Key frame text model of the
Configuration page's Amazon Bedrock Models section), which says what the
frame shows (``ContentKind``: SCREEN for shared content, CAMERA for the
meeting's camera video, MIXED for both), names the shared content in a few
words (``Title``), and transcribes its readable text, noting any chart or
picture in a line; the text is stored on the frame as ``SlideText``. The
meeting assistant reads it before it looks at a picture, the meeting page
searches it, and the meeting's document in the meeting-history knowledge base
includes it. A CAMERA frame is stored with no text and no title, and the
readers leave it out (docs/meeting-video.md, "Camera frames").

Once read, a frame is compared with the last frame before it that is neither a
camera frame nor itself a repeat; when the two show the same screen (the same
text, near enough, or the same title and much the same text) it is stored with
``DuplicateOf`` naming that frame, and the meeting page hides it by default
(docs/meeting-video.md, "Repeated frames").

A vision model rather than Amazon Textract: on the measured frames both read
every word, and the model also says what a chart shows, for a tenth of the
price per frame (docs/meeting-video.md, "Slide text").

Idempotent: a frame that already has its text is left alone, so a redelivered
event costs nothing. An AWS error or an answer with no text is logged and ends
the invocation without an error; anything else ends it with one. Either way the
function's asynchronous invoke allows no retries, so a model call is never
repeated, and the frame simply has no text, which the readers handle.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

import boto3
import pure
from bedrock_compat import TRUNCATED_STOP_REASON, build_inference_config, extract_text
from botocore.config import Config
from botocore.exceptions import ClientError

# pylint: disable=import-error
from lma_config import (  # configuration layer
    LANGUAGE_MODELS,
    read_setting,
    resolve_model_id,
)
from lma_metering import MeteringWriter, bedrock_service, bedrock_usage  # configuration layer

# pylint: enable=import-error

# Standard logging rather than Powertools, as in the agenda deriver: the layer
# that carries Powertools is built for arm64 only.
LOGGER = logging.getLogger()
LOGGER.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

EVENT_SOURCING_TABLE = os.environ.get("EVENT_SOURCING_TABLE", "")
RECORDINGS_BUCKET = os.environ.get("RECORDINGS_BUCKET", "")
KEYFRAMES_PREFIX = os.environ.get("KEYFRAMES_PREFIX", "lma-video-keyframes/")
#: The model used when the configuration cannot be read; the same value as the
#: setting's shipped default in setting_defaults.json.
FALLBACK_MODEL_ID = "amazon.nova-lite-v1:0"
REGION = os.environ.get("AWS_REGION", "us-east-1")

#: Room for a dense page of text (about 2,000 characters, 600 tokens) and a
#: line about a chart. Kept close to that: an answer that reaches it is almost
#: always a model repeating itself, and every token of the loop is billed.
MAX_OUTPUT_TOKENS = 1200
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


#: Frames before this one looked at to find the one it may repeat. Past a run
#: of camera frames and repeats this long, the frame is taken as new.
EARLIER_FRAMES = 20


def duplicate_of(call_id: str, frame_id: str, title: str, text: str) -> Optional[str]:
    """The FrameId of the earlier frame this one repeats, or None.

    None as well when the earlier frames cannot be read: a frame not marked is
    shown, which is how every frame was shown before frames were compared.
    """
    try:
        items = DYNAMODB.query(
            TableName=EVENT_SOURCING_TABLE,
            KeyConditionExpression="PK = :pk AND SK < :sk",
            ExpressionAttributeValues={
                ":pk": {"S": f"kf#{call_id}"},
                ":sk": {"S": f"f#{frame_id}"},
            },
            ProjectionExpression="#f, #t, #k, #n, #d",
            ExpressionAttributeNames={
                "#f": "FrameId",
                "#t": "SlideText",
                "#k": "ContentKind",
                "#n": "Title",
                "#d": "DuplicateOf",
            },
            ScanIndexForward=False,
            Limit=EARLIER_FRAMES,
        ).get("Items", [])
    except ClientError as error:
        LOGGER.warning(
            "Could not read the frames before key frame %s: %s",
            frame_id,
            error.response["Error"]["Code"],
        )
        return None
    earlier = pure.earlier_original(items)
    if earlier is None:
        return None
    earlier_id = (earlier.get("FrameId") or {}).get("S")
    if not earlier_id:
        return None
    earlier_title = (earlier.get("Title") or {}).get("S")
    earlier_text = (earlier.get("SlideText") or {}).get("S")
    return earlier_id if pure.is_duplicate(title, text, earlier_title, earlier_text) else None


def model_id() -> str:
    """The model that reads key frames.

    The Amazon Bedrock Models section's value on the Configuration page, else
    the shipped default in setting_defaults.json, run within the section's
    Model inference geography.
    """
    return resolve_model_id(
        read_setting(
            LANGUAGE_MODELS.name,
            "keyframeTextModelId",
            default=FALLBACK_MODEL_ID,
        ),
        region=REGION,
    )


def read_text(image: bytes, call_id: str, model: str) -> Tuple[str, str, str]:
    """The frame's ContentKind, Title and text, from the model, metered against the meeting."""
    response = BEDROCK.converse(
        modelId=model,
        messages=[
            {
                "role": "user",
                "content": [
                    {"image": {"format": "jpeg", "source": {"bytes": image}}},
                    {"text": pure.PROMPT},
                ],
            }
        ],
        inferenceConfig=build_inference_config(model, MAX_OUTPUT_TOKENS),
    )
    METERING.record(
        call_id, "SlideText", bedrock_service(model), bedrock_usage(response.get("usage"))
    )
    truncated = response.get("stopReason") == TRUNCATED_STOP_REASON
    if truncated:
        # Suspect: at this budget the model has usually been looping. What is
        # left once clean_text has collapsed the loop is kept.
        LOGGER.warning(
            "The answer for a key frame of %s reached %d tokens", call_id, MAX_OUTPUT_TOKENS
        )
    return pure.read_answer(extract_text(response), truncated=truncated)


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
        model = model_id()
        kind, title, text = read_text(body, call_id, model)
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

    update = "SET SlideText = :t, ContentKind = :k, SlideTextModel = :m, UpdatedAt = :u"
    values = {
        ":t": {"S": text},
        ":k": {"S": kind},
        ":m": {"S": model},
        ":u": {"S": datetime.now(timezone.utc).isoformat()},
    }
    if title:
        update += ", Title = :n"
        values[":n"] = {"S": title}
    original = duplicate_of(call_id, frame_id, title, text) if kind != pure.CAMERA else None
    if original:
        update += ", DuplicateOf = :d"
        values[":d"] = {"S": original}
    try:
        DYNAMODB.update_item(
            TableName=EVENT_SOURCING_TABLE,
            Key=frame_key(call_id, frame_id),
            UpdateExpression=update,
            # Not onto a frame deleted with its meeting meanwhile.
            ConditionExpression="attribute_exists(PK)",
            ExpressionAttributeValues=values,
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
    LOGGER.info(
        "Read key frame %s of %s: %s, %d characters%s",
        frame_id,
        call_id,
        kind,
        len(text),
        f", repeating {original}" if original else "",
    )
    result = {"status": "read", "characters": len(text), "contentKind": kind}
    if original:
        result["duplicateOf"] = original
    return result
