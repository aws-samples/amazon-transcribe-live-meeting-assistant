#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""Check one consent evidence screenshot with a vision model.

Invoked synchronously by the meeting consent resolver for
``verifyMeetingConsentEvidence``, which has already applied the caller's access
rule, read the meeting's consent record and found the evidence entry; it sends
the image's key and what the model needs to judge it, and stores what this
returns on the entry. This function reads the image, makes one model call,
meters it against the meeting, and returns the parsed answer and verdict. It
writes nothing to the meeting. See docs/meeting-consent.md.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict

import boto3
import pure
from botocore.config import Config
from botocore.exceptions import ClientError

# pylint: disable=import-error
from lma_metering import MeteringWriter, bedrock_service, bedrock_usage  # configuration layer

# pylint: enable=import-error

LOGGER = logging.getLogger()
LOGGER.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

RECORDINGS_BUCKET = os.environ.get("RECORDINGS_BUCKET", "")
EVIDENCE_PREFIX = os.environ.get("CONSENT_EVIDENCE_PREFIX", "lma-consent-evidence/")
BEDROCK_REGION = os.environ.get("BEDROCK_REGION_OVERRIDE") or os.environ.get("AWS_REGION", "")
#: The model when the request names none: the Meeting Consent section's default.
FALLBACK_MODEL_ID = "amazon.nova-lite-v1:0"
#: Bedrock Converse refuses an image larger than this.
MAX_IMAGE_BYTES = 3_750_000
MAX_OUTPUT_TOKENS = 1500

S3 = boto3.client("s3", config=Config(retries={"mode": "adaptive", "max_attempts": 3}))
BEDROCK = boto3.client(
    "bedrock-runtime",
    region_name=BEDROCK_REGION or None,
    config=Config(retries={"mode": "adaptive", "max_attempts": 50}),
)
# One model call per check, billed to the meeting. Inert when METERING_TABLE_NAME
# is unset.
METERING = MeteringWriter(source="consent-evidence-verifier")


class VerificationError(Exception):
    """The evidence could not be checked; the message says why."""


def posixify_filename(filename: str) -> str:
    """The folder name producers use for a meeting; the same rule as meeting deletion."""
    name = re.sub(r"[^a-zA-Z0-9_.]", "_", filename)
    name = re.sub(r"^_+", "", name)
    return re.sub(r"_+$", "", name)


def read_image(call_id: str, key: str) -> bytes:
    """The image, only from the meeting's own evidence folder."""
    folder = f"{EVIDENCE_PREFIX}{posixify_filename(call_id)}/"
    if not isinstance(key, str) or not key.startswith(folder) or ".." in key:
        raise VerificationError("the evidence is not in this meeting's evidence folder")
    try:
        head = S3.head_object(Bucket=RECORDINGS_BUCKET, Key=key)
    except ClientError as error:
        raise VerificationError("the evidence image could not be read") from error
    if int(head.get("ContentLength") or 0) > MAX_IMAGE_BYTES:
        raise VerificationError("the evidence image is larger than the model accepts (3.75 MB)")
    return S3.get_object(Bucket=RECORDINGS_BUCKET, Key=key)["Body"].read()


def verify(event: Dict[str, Any]) -> Dict[str, Any]:
    """Check one image; the result in the shape stored as ``Verification``."""
    call_id = str(event.get("CallId") or "")
    if not call_id:
        raise VerificationError("no CallId")
    data = read_image(call_id, str(event.get("S3Key") or ""))
    image_format = pure.image_format(data)
    if image_format is None:
        raise VerificationError("the evidence is not a JPEG, PNG, GIF or WebP image")
    model_id = event.get("ModelId") or FALLBACK_MODEL_ID
    participants = [str(p) for p in event.get("Participants") or []]
    prompt = pure.build_prompt(
        event.get("NoticeText"),
        event.get("Mode"),
        participants,
        event.get("ConsentPhrases") or [],
        event.get("ObjectionPhrases") or [],
    )
    response = BEDROCK.converse(
        modelId=model_id,
        messages=[
            {
                "role": "user",
                "content": [
                    {"image": {"format": image_format, "source": {"bytes": data}}},
                    {"text": prompt},
                ],
            }
        ],
        inferenceConfig={"maxTokens": MAX_OUTPUT_TOKENS, "temperature": 0},
    )
    usage = response.get("usage") or {}
    METERING.record(
        call_id,
        "ConsentVerification",
        bedrock_service(model_id),
        bedrock_usage(usage),
        # Recorded against the region the model ran in when that is not the
        # stack's own. The stack sets the override to its own region today; the
        # comparison keeps metering right if it is ever pointed elsewhere.
        region=BEDROCK_REGION if BEDROCK_REGION != os.environ.get("AWS_REGION") else None,
    )
    text = "".join(
        block.get("text", "")
        for block in ((response.get("output") or {}).get("message") or {}).get("content") or []
    )
    parsed = pure.parse_answer(text)
    judged = pure.verdict(parsed, event.get("Mode"), event.get("Rule"), participants)
    # Sizes and the verdict, never the content: names and replies are meeting data.
    LOGGER.info(
        "verified consent evidence of %s with %s: readable=%s verdict=%s replies=%d",
        call_id,
        model_id,
        parsed["Readable"],
        judged["Verdict"],
        len(parsed["Replies"]),
    )
    return {
        **judged,
        **parsed,
        "ModelId": model_id,
        "VerifiedAt": datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z"),
        "InputTokens": int(usage.get("inputTokens") or 0),
        "OutputTokens": int(usage.get("outputTokens") or 0),
    }


def handler(event: Dict[str, Any], _context: Any) -> Dict[str, Any]:
    """Entry point for the resolver's synchronous invoke.

    Answers ``{"Ok": true, "Verification": {...}}`` or ``{"Ok": false,
    "Message": ...}``; a model or S3 failure is reported as a message rather
    than raised, so the resolver can tell the caller what happened.
    """
    try:
        return {"Ok": True, "Verification": verify(event)}
    except VerificationError as error:
        LOGGER.info("consent evidence of %s not checked: %s", event.get("CallId"), error)
        return {"Ok": False, "Message": str(error)}
    except Exception:  # noqa: BLE001 - reported to the caller, see the docstring
        LOGGER.exception("consent evidence check of %s failed", event.get("CallId"))
        return {"Ok": False, "Message": "the evidence could not be checked just now; try again"}
