# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Derive a meeting's agenda from its transcript, when one was stated.

Reachable two ways, both doing the same work:

* **Automatically, once per meeting** -- invoked asynchronously by the call
  event processor when a meeting with no agenda reaches its fifth minute or so.
  The pass first claims the meeting with a conditional write, so of the several
  invocations a busy stream can produce only one reaches the model, and it
  stores what it finds only while the meeting still has no agenda
  (``OnlyIfUnset``), so an agenda somebody set meanwhile always wins.
* **On demand** -- the Agenda panel's "Derive agenda" button, through the
  ``deriveCallAgenda`` AppSync mutation. The caller's own identity is checked
  here, because a Lambda resolver has no DynamoDB condition to lean on. This
  replaces whatever agenda is stored, which the page confirms first.

Either way an agenda is stored only if one was actually stated; see pure.py for
how that is held. Segments are read straight from DynamoDB.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

import boto3
import pure
import requests
from bedrock_compat import SUMMARY_MAX_TOKENS, build_inference_config, extract_text
from boto3.dynamodb.types import TypeDeserializer
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.config import Config
from botocore.exceptions import ClientError

# pylint: disable=import-error
from lma_config import LANGUAGE_MODELS, read_setting  # configuration layer
from lma_metering import MeteringWriter, bedrock_service, bedrock_usage  # configuration layer

# pylint: enable=import-error

# Standard logging rather than Powertools, for the same reason as the speaker
# name resolver: the layer that carries Powertools is built for arm64 only.
LOGGER = logging.getLogger()
LOGGER.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

EVENT_SOURCING_TABLE = os.environ["EVENT_SOURCING_TABLE"]
APPSYNC_GRAPHQL_URL = os.environ["APPSYNC_GRAPHQL_URL"]
BEDROCK_REGION = os.environ.get("BEDROCK_REGION_OVERRIDE") or os.environ["AWS_REGION"]

# Same page cap and reasoning as FetchTranscript: a table that keeps handing back
# a continuation key must not spin this function for its whole timeout.
MAX_TRANSCRIPT_PAGES = 100

#: Who an automatic pass's agenda is attributed to on the meeting page.
AUTOMATIC_ATTRIBUTION = "LMA (derived from the transcript)"

#: The attribute the automatic pass claims a meeting with. Internal bookkeeping on
#: the meeting record, not part of the API: it is written here, directly, and read
#: only by the condition below.
CLAIM_ATTRIBUTE = "AgendaDerivationAttemptedAt"

# The same full Call field set onUpdateCall selects. AppSync publishes a
# mutation's own response to that subscription, and a field it selects that the
# mutation did not arrives as null in every open meeting page.
_CALL_FIELDS = """
      PK
      SK
      CallId
      AgentId
      CallCategories
      IssuesDetected
      CallSummaryText
      CallSummaryState
      SpeakerIdentities
      Agenda
      AgendaSource
      AgendaUpdatedBy
      AgendaUpdatedAt
      Status
      UpdatedAt
      CreatedAt
      CustomerPhoneNumber
      SystemPhoneNumber
      RecordingUrl
      VideoRecordingUrl
      PcaUrl
      Owner
      SharedWith
      TotalConversationDurationMillis
      Sentiment {
        OverallSentiment { AGENT CALLER }
        SentimentByPeriod {
          QUARTER {
            AGENT { BeginOffsetMillis EndOffsetMillis Score }
            CALLER { BeginOffsetMillis EndOffsetMillis Score }
          }
        }
      }
"""

DESERIALIZER = TypeDeserializer()

dynamodb = boto3.client("dynamodb")
# One model call per pass, billed to the meeting. Inert when METERING_TABLE_NAME
# is unset.
METERING = MeteringWriter(source="agenda-deriver", client=dynamodb)
bedrock = boto3.client(
    service_name="bedrock-runtime",
    region_name=BEDROCK_REGION,
    config=Config(retries={"max_attempts": 50, "mode": "adaptive"}),
)


def _deserialize(item):
    return {key: DESERIALIZER.deserialize(value) for key, value in item.items()}


def _meeting_key(call_id):
    return {"PK": {"S": f"c#{call_id}"}, "SK": {"S": f"c#{call_id}"}}


def read_meeting(call_id):
    """The meeting row: its agenda and the access fields."""
    item = dynamodb.get_item(TableName=EVENT_SOURCING_TABLE, Key=_meeting_key(call_id)).get("Item")
    return _deserialize(item) if item else None


def read_segments(call_id):
    """Every transcript segment for a meeting, across all result pages."""
    items = []
    start_key = None
    pages = 0
    while pages < MAX_TRANSCRIPT_PAGES:
        kwargs = {
            "TableName": EVENT_SOURCING_TABLE,
            "KeyConditionExpression": "PK = :pk",
            "ExpressionAttributeValues": {":pk": {"S": f"trs#{call_id}"}},
        }
        if start_key:
            kwargs["ExclusiveStartKey"] = start_key
        response = dynamodb.query(**kwargs)
        items.extend(_deserialize(item) for item in response.get("Items", []))
        pages += 1
        next_key = response.get("LastEvaluatedKey")
        if not next_key or next_key == start_key:
            break
        start_key = next_key
    return items


# BEGIN shared_with_entries -- one text in every Lambda that reads SharedWith;
# lma-ai-stack/test/test_shared_with_access.py holds the copies to it.
# The entries of a SharedWith value, read as the AppSync resolvers read them
# (the "SharedWith entries" VTL block and sharedWithEntries in the JS
# resolvers): a list, tuple or set holds one entry per string element, and a
# string -- comma-separated or "[a, b]" -- is split at every "[", "]" and ",".
# Those three characters only ever separate entries, so a list element that
# contains one is not an entry. Entries are trimmed and empty ones dropped;
# membership is then a comparison of whole entries.
def shared_with_entries(value: object) -> list:
    """The trimmed, non-empty entries of a SharedWith value."""
    if isinstance(value, str):
        parts = value.replace("[", ",").replace("]", ",").split(",")
    elif isinstance(value, (list, tuple, set, frozenset)):
        parts = [
            part
            for part in value
            if isinstance(part, str) and not any(separator in part for separator in "[],")
        ]
    else:
        return []
    return [part.strip() for part in parts if part.strip()]


# END shared_with_entries


def caller_may_edit(meeting, identity):
    """The same rule the read path and the direct-write resolvers apply.

    Admin, the meeting's owner, or a user it has been shared with. An invocation
    with no Cognito identity is the backend calling itself (the automatic pass)
    and is allowed. Matches `caller_may_edit` in speaker_name_resolver/index.py
    and summary_regenerator/index.py, which explain the two shapes `SharedWith`
    is stored in.
    """
    if not identity:
        return True
    username = identity.get("username") or identity.get("sub")
    groups = identity.get("groups") or []
    if "Admin" in groups:
        return True
    if not username:
        return False
    if meeting.get("Owner") == username:
        return True
    return username in shared_with_entries(meeting.get("SharedWith"))


def claim_automatic_pass(call_id, now_iso):
    """Record that the automatic pass ran for this meeting; False if it already had.

    One conditional write, so however many invocations the event processor
    sends -- one per execution environment that sees the meeting pass its fifth
    minute -- only one of them goes on to pay for a model call. The condition
    also declines a meeting that has gained an agenda in the meantime.
    """
    try:
        dynamodb.update_item(
            TableName=EVENT_SOURCING_TABLE,
            Key=_meeting_key(call_id),
            UpdateExpression="SET #claim = :now",
            ConditionExpression=(
                "attribute_exists(PK) AND attribute_not_exists(#claim) "
                "AND (attribute_not_exists(#agenda) OR #agenda = :empty)"
            ),
            ExpressionAttributeNames={"#claim": CLAIM_ATTRIBUTE, "#agenda": "Agenda"},
            ExpressionAttributeValues={":now": {"S": now_iso}, ":empty": {"S": ""}},
        )
        return True
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return False
        raise


def call_bedrock(prompt, call_id):
    """One model call, metered against the meeting.

    Temperature 0 where the model accepts it: this is extraction. Sized like a
    summary section, which reads a transcript of the same order. The model is the
    summaries' one: the Amazon Bedrock Models section of the Configuration page,
    whose default is the BedrockModelId stack parameter.
    """
    model_id = read_setting(LANGUAGE_MODELS.name, "summaryModelId", env_var="BEDROCK_MODEL_ID")
    response = bedrock.converse(
        modelId=model_id,
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        inferenceConfig=build_inference_config(model_id, SUMMARY_MAX_TOKENS),
    )
    METERING.record(
        call_id,
        "AgendaDerivation",
        bedrock_service(model_id),
        bedrock_usage(response.get("usage")),
        region=BEDROCK_REGION if BEDROCK_REGION != os.environ.get("AWS_REGION") else None,
    )
    return extract_text(response)


class AgendaAlreadySet(Exception):
    """The OnlyIfUnset condition refused the write: somebody set one first."""


def write_agenda(call_id, agenda, updated_by, only_if_unset):
    """Store the agenda through AppSync, so open meeting pages update live."""
    query = (
        "mutation UpdateCallAgenda($input: UpdateCallAgendaInput!) {\n"
        "  updateCallAgenda(input: $input) {" + _CALL_FIELDS + "  }\n}"
    )
    payload = json.dumps(
        {
            "query": query,
            "variables": {
                "input": {
                    "CallId": call_id,
                    "Agenda": agenda,
                    "AgendaSource": "derived",
                    "AgendaUpdatedBy": updated_by,
                    "OnlyIfUnset": only_if_unset,
                }
            },
        }
    )
    session = boto3.Session()
    request = AWSRequest(
        method="POST",
        url=APPSYNC_GRAPHQL_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    SigV4Auth(session.get_credentials(), "appsync", session.region_name).add_auth(request)
    response = requests.post(
        APPSYNC_GRAPHQL_URL, data=payload, headers=dict(request.headers), timeout=20
    )
    body = response.json()
    errors = body.get("errors") or []
    if only_if_unset and any("condition failure" in str(e.get("message")) for e in errors):
        raise AgendaAlreadySet()
    if errors:
        raise RuntimeError(f"updateCallAgenda failed: {errors}")
    return body


def derive(call_id, identity=None, automatic=False):
    """Run one pass. Returns a small result object for the caller."""
    meeting = read_meeting(call_id)
    if meeting is None:
        return {"ok": False, "reason": "no such meeting", "derived": False}
    if not caller_may_edit(meeting, identity):
        return {"ok": False, "reason": "not permitted", "derived": False}

    if automatic:
        if pure.has_agenda(meeting):
            return {"ok": True, "reason": "the meeting already has an agenda", "derived": False}
        if not claim_automatic_pass(call_id, datetime.now(timezone.utc).isoformat()):
            LOGGER.info("Agenda pass for %s already ran, or the meeting has an agenda", call_id)
            return {"ok": True, "reason": "already attempted", "derived": False}

    transcript = pure.build_transcript(read_segments(call_id))
    if not transcript:
        return {"ok": True, "reason": "nothing has been said in the meeting yet", "derived": False}

    raw = call_bedrock(pure.build_prompt(transcript), call_id)
    items = pure.parse_model_response(raw, transcript)
    # The decision and its size, never the content: the transcript and the
    # agenda are meeting content.
    LOGGER.info(
        "Agenda pass for %s (automatic=%s): transcript=%d chars, stated=%s, items=%d",
        call_id,
        automatic,
        len(transcript),
        items is not None,
        len(items or []),
    )
    if not items:
        return {"ok": True, "reason": "no agenda was stated in the meeting", "derived": False}

    updated_by = AUTOMATIC_ATTRIBUTION
    if identity:
        updated_by = identity.get("username") or identity.get("sub") or AUTOMATIC_ATTRIBUTION
    try:
        write_agenda(call_id, pure.agenda_text(items), updated_by, only_if_unset=automatic)
    except AgendaAlreadySet:
        LOGGER.info("An agenda was set on %s while the automatic pass ran; keeping it", call_id)
        return {"ok": True, "reason": "an agenda was set while this ran", "derived": False}
    noun = "item" if len(items) == 1 else "items"
    return {"ok": True, "reason": f"found a stated agenda of {len(items)} {noun}", "derived": True}


def handler(event, context):  # pylint: disable=unused-argument
    """Entry point for both the AppSync resolver and the backend invoke."""
    arguments = (event.get("arguments") or {}).get("input") or {}
    call_id = arguments.get("CallId") or event.get("CallId")
    if not call_id:
        raise ValueError("no CallId in event")

    identity = event.get("identity")
    # Only the backend can ask for the automatic pass: a browser caller always
    # arrives with an identity, and its press is the on-demand pass.
    automatic = identity is None and event.get("Trigger") == "auto"
    LOGGER.debug("Agenda deriver event for %s, automatic=%s", call_id, automatic)

    try:
        result = derive(call_id, identity=identity, automatic=automatic)
    except Exception:  # noqa: BLE001 - reported to the caller, see below
        # The automatic pass is an enhancement and its caller does not wait on
        # it; the button gets a message rather than AppSync and DynamoDB
        # internals in a browser.
        LOGGER.exception("Agenda pass failed for %s", call_id)
        return {
            "Success": False,
            "Message": "could not derive an agenda just now",
            "Derived": False,
        }

    return {"Success": result["ok"], "Message": result["reason"], "Derived": result["derived"]}
