# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Give a stored chat attachment its AI title and summary, once.

Invoked asynchronously by the call event processor for each chat attachment it
stores with ``Summarize`` set (docs/virtual-participant.md, "Chat attachments").
The attachment is read from the recordings bucket and given to the meeting
assistant's model (the Meeting assistant model of the Configuration page's
Amazon Bedrock Models section) with Amazon Bedrock Converse: an image as an
image block, a PDF, Word, Excel, text, CSV or Markdown file as a document block.
The model answers with a short ``Title`` and a one or two sentence ``Summary``,
stored on the attachment with ``SummaryState`` DONE. The call is metered
against the meeting under the ChatAttachments context.

An attachment the model cannot read -- a PowerPoint file, which Converse has no
document format for, or one over the size Converse accepts -- is marked SKIPPED
without a model call, and is shown by its file name. A model error, or an
answer with neither a title nor a summary, marks it FAILED.

Idempotent: an attachment that already has a ``SummaryState`` is left alone, so
a redelivered event costs nothing, and the function's asynchronous invoke
allows no retries, so a model call is never repeated and metered twice.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import boto3
import pure
from bedrock_compat import (
    TRUNCATED_STOP_REASON,
    additional_request_fields,
    build_inference_config,
    extract_text,
)
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

# Standard logging rather than Powertools, as in the key frame text extractor:
# the layer that carries Powertools is built for arm64 only.
LOGGER = logging.getLogger()
LOGGER.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

EVENT_SOURCING_TABLE = os.environ.get("EVENT_SOURCING_TABLE", "")
RECORDINGS_BUCKET = os.environ.get("RECORDINGS_BUCKET", "")
ATTACHMENTS_PREFIX = os.environ.get("ATTACHMENTS_PREFIX", "lma-chat-attachments/")
#: The model used when the configuration cannot be read; the same value as the
#: setting's shipped default in setting_defaults.json.
FALLBACK_MODEL_ID = "global.anthropic.claude-haiku-5-5"
REGION = os.environ.get("AWS_REGION", "us-east-1")

#: The output budgets, first try and retry. The answer itself is a title and
#: two sentences of JSON, under 150 tokens, but a reasoning model (the meeting
#: assistant's model may be one) spends its reasoning from the same budget
#: before it writes anything: at 400 a Claude 5 model was seen to stop at the
#: ceiling with no answer. maxTokens is a ceiling, not a reservation, so a model
#: that does not reason is billed for its short answer whichever is offered.
MAX_OUTPUT_TOKENS = 2048
RETRY_OUTPUT_TOKENS = 8192
#: Asked of the models that take a reasoning effort: naming and summarizing one
#: file needs little reasoning.
REASONING_EFFORT = "low"

#: The context the model call is metered under, in the meeting's cost estimate.
METERING_CONTEXT = "ChatAttachments"

DONE, FAILED, SKIPPED = "DONE", "FAILED", "SKIPPED"

CLIENT_CONFIG = Config(retries={"mode": "adaptive", "max_attempts": 3})
DYNAMODB = boto3.client("dynamodb", config=CLIENT_CONFIG)
S3 = boto3.client("s3", config=CLIENT_CONFIG)
#: Seconds a model call may take to answer, and the attempts botocore makes for
#: one call, the first included. The key frame text extractor allows 50
#: attempts for bursts of throttling, but botocore retries a read timeout the
#: same way, and a retried call is a model call the metering never sees. So the
#: attempts are few, sized with the read timeout to fit the function's timeout
#: (240 s in the template): two calls (the second with the larger budget), each
#: of at most CALL_ATTEMPTS attempts of READ_TIMEOUT_SECONDS. A call still
#: throttled after them marks the attachment FAILED.
READ_TIMEOUT_SECONDS = 55
CALL_ATTEMPTS = 2
BEDROCK = boto3.client(
    "bedrock-runtime",
    region_name=REGION,
    config=Config(
        connect_timeout=5,
        read_timeout=READ_TIMEOUT_SECONDS,
        retries={"mode": "adaptive", "total_max_attempts": CALL_ATTEMPTS},
    ),
)
METERING = MeteringWriter(source="chat-attachment-summarizer")


def attachment_key(call_id: str, attachment_id: str) -> Dict[str, Any]:
    return {"PK": {"S": f"att#{call_id}"}, "SK": {"S": f"a#{attachment_id}"}}


def model_id() -> str:
    """The meeting assistant's model.

    The Amazon Bedrock Models section's value on the Configuration page, else
    the shipped default in setting_defaults.json, run within the section's
    Model inference geography.
    """
    return resolve_model_id(
        read_setting(
            LANGUAGE_MODELS.name,
            "meetingAssistModelId",
            default=FALLBACK_MODEL_ID,
        ),
        region=REGION,
    )


def _string(item: Dict[str, Any], name: str) -> Optional[str]:
    return (item.get(name) or {}).get("S")


def record(call_id: str, attachment_id: str, state: str, extra: Dict[str, str]) -> str:
    """Store the outcome on the attachment, unless it already has one or is gone."""
    update = "SET SummaryState = :s, UpdatedAt = :u"
    values: Dict[str, Any] = {
        ":s": {"S": state},
        ":u": {"S": datetime.now(timezone.utc).isoformat()},
    }
    for index, (name, value) in enumerate(extra.items()):
        update += f", {name} = :v{index}"
        values[f":v{index}"] = {"S": value}
    try:
        DYNAMODB.update_item(
            TableName=EVENT_SOURCING_TABLE,
            Key=attachment_key(call_id, attachment_id),
            UpdateExpression=update,
            # Not onto an attachment deleted with its meeting or transcript
            # meanwhile, and once only.
            ConditionExpression="attribute_exists(PK) AND attribute_not_exists(SummaryState)",
            ExpressionAttributeValues=values,
        )
    except ClientError as error:
        if error.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return "skipped"
        LOGGER.error(
            "Could not store the outcome for chat attachment %s: %s",
            attachment_id,
            error.response["Error"]["Code"],
        )
        return "failed"
    return state.lower()


def _converse(content_type: str, file_name: Optional[str], body: bytes, model: str, budget: int):
    request: Dict[str, Any] = {
        "modelId": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    pure.content_block(content_type, body),
                    {"text": pure.prompt_for(content_type, file_name)},
                ],
            }
        ],
        "inferenceConfig": build_inference_config(model, budget),
    }
    extra = additional_request_fields(model, REASONING_EFFORT)
    if extra:
        request["additionalModelRequestFields"] = extra
    return BEDROCK.converse(**request)


def summarize(content_type: str, file_name: Optional[str], body: bytes, call_id: str, model: str):
    """The attachment's title and summary, from the model, metered against the meeting.

    An answer cut off at the output budget, and an answer with no text at all,
    is asked for once more with a larger budget. Each call is metered. A second
    failure raises ValueError, as any answer without a title or summary does.
    """
    for attempt, budget in enumerate((MAX_OUTPUT_TOKENS, RETRY_OUTPUT_TOKENS)):
        response = _converse(content_type, file_name, body, model, budget)
        METERING.record(
            call_id, METERING_CONTEXT, bedrock_service(model), bedrock_usage(response.get("usage"))
        )
        # Worth asking again: an answer cut off at the budget, or one with no
        # text (a reasoning model that spent the budget reasoning, or an empty
        # text block).
        retry = response.get("stopReason") == TRUNCATED_STOP_REASON
        try:
            text = extract_text(response)
        except ValueError:
            text = ""
        if not text.strip():
            retry = True
        else:
            try:
                return pure.read_answer(text)
            except ValueError:
                if not retry:
                    raise
        if attempt:
            raise ValueError("no title or summary within the larger output budget")
        LOGGER.warning(
            "The answer for a chat attachment of %s was cut off or empty at %d output "
            "tokens; asking once more with %d",
            call_id,
            budget,
            RETRY_OUTPUT_TOKENS,
        )
    raise ValueError("no title or summary within the output budget")


def handler(event: Dict[str, Any], _context: Any) -> Dict[str, Any]:
    request = pure.request_from(event or {})
    if request is None:
        LOGGER.warning("Not summarizing: the request has no usable CallId and AttachmentId")
        return {"status": "skipped", "reason": "bad request"}
    call_id, attachment_id = request["CallId"], request["AttachmentId"]

    try:
        item = DYNAMODB.get_item(
            TableName=EVENT_SOURCING_TABLE,
            Key=attachment_key(call_id, attachment_id),
            ProjectionExpression="#st, Summarize, SummaryState, S3Key, ContentType, FileName",
            ExpressionAttributeNames={"#st": "Status"},
        ).get("Item")
    except ClientError as error:
        LOGGER.error(
            "Could not read chat attachment %s: %s", attachment_id, error.response["Error"]["Code"]
        )
        return {"status": "failed", "reason": "attachment unreadable"}
    if item is None:
        LOGGER.info("Chat attachment %s is not stored (deleted, or not yet written)", attachment_id)
        return {"status": "skipped", "reason": "no attachment"}
    if "SummaryState" in item:
        return {"status": "skipped", "reason": "already summarized"}
    if _string(item, "Status") != "STORED" or not (item.get("Summarize") or {}).get("BOOL"):
        return {"status": "skipped", "reason": "not to be summarized"}

    key = pure.object_key(_string(item, "S3Key"), call_id, ATTACHMENTS_PREFIX)
    if key is None:
        LOGGER.warning(
            "Not summarizing chat attachment %s: it is not in the meeting's folder", attachment_id
        )
        return {"status": "skipped", "reason": "outside the meeting's folder"}
    content_type = _string(item, "ContentType") or ""

    try:
        head = S3.head_object(Bucket=RECORDINGS_BUCKET, Key=key)
    except ClientError as error:
        LOGGER.error(
            "Could not read chat attachment %s: %s", attachment_id, error.response["Error"]["Code"]
        )
        return {
            "status": record(call_id, attachment_id, FAILED, {}),
            "reason": "content unreadable",
        }
    reason = pure.readable(content_type, int(head.get("ContentLength") or 0))
    if reason:
        LOGGER.info("Not summarizing chat attachment %s: %s", attachment_id, reason)
        return {"status": record(call_id, attachment_id, SKIPPED, {}), "reason": reason}

    try:
        body = S3.get_object(Bucket=RECORDINGS_BUCKET, Key=key)["Body"].read()
        model = model_id()
        title, summary = summarize(content_type, _string(item, "FileName"), body, call_id, model)
    except ClientError as error:
        LOGGER.error(
            "The model could not summarize chat attachment %s: %s",
            attachment_id,
            error.response["Error"]["Code"],
        )
        return {"status": record(call_id, attachment_id, FAILED, {}), "reason": "model error"}
    except ValueError as error:
        LOGGER.error(
            "The model gave no title or summary for chat attachment %s: %s", attachment_id, error
        )
        return {"status": record(call_id, attachment_id, FAILED, {}), "reason": "no answer"}

    extra = {"SummaryModel": model}
    if title:
        extra["Title"] = title
    if summary:
        extra["Summary"] = summary
    status = record(call_id, attachment_id, DONE, extra)
    LOGGER.info(
        "Summarized chat attachment %s of %s: %d characters", attachment_id, call_id, len(summary)
    )
    return {"status": status}
