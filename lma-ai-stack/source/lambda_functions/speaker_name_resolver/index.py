# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Infer who each separated voice belongs to, from what people say.

Reachable three ways, all the same work:

* **On demand** -- the Speakers panel's "Identify speakers" button, through the
  `resolveSpeakerNames` AppSync mutation. The caller's own identity is checked
  here, because a Lambda resolver has no DynamoDB condition to lean on.
* **While a meeting runs** -- the same mutation, called by the meeting page on a
  slow cadence while voices remain unnamed. There is no backend scheduler: the
  work only happens when somebody is there to see it, and the end-of-meeting
  pass covers the case where nobody is.
* **At the end of a meeting** -- invoked by the call event processor, before the
  summary is triggered, so the summary reads the names rather than voice ids.
  This is the highest-accuracy pass because it sees the whole transcript.

Segments are read straight from DynamoDB rather than through FetchTranscript:
that function now resolves names, which is exactly the labels this needs to map.

All of the judgement lives in pure.py; this module is the AWS plumbing.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

import boto3
import pure
import requests
from boto3.dynamodb.types import TypeDeserializer
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.config import Config

# Standard logging rather than Powertools: the layer that carries Powertools is
# built for arm64 only, and this function has no other reason to be pinned to an
# architecture. Same choice as FetchTranscript.
LOGGER = logging.getLogger()
LOGGER.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

EVENT_SOURCING_TABLE = os.environ["EVENT_SOURCING_TABLE"]
APPSYNC_GRAPHQL_URL = os.environ["APPSYNC_GRAPHQL_URL"]
BEDROCK_MODEL_ID = os.environ["BEDROCK_MODEL_ID"]
BEDROCK_REGION = os.environ.get("BEDROCK_REGION_OVERRIDE") or os.environ["AWS_REGION"]

# Upper bound on turns handed to the model. A long meeting's whole transcript
# would be both slow and mostly expensive for no gain -- naming evidence
# clusters at the start, where people introduce themselves. Past this limit the
# opening turns and the most recent ones are kept; see select_turns for why both.
MAX_TURNS = int(os.environ.get("SPEAKER_NAMING_MAX_TURNS", "160"))

# Same page cap and reasoning as FetchTranscript: a table that keeps handing back
# a continuation key must not spin this function for its whole timeout.
MAX_TRANSCRIPT_PAGES = 100

# Shortest gap between two passes on one meeting. The timer that drives automatic
# passes lives in the browser, and a client cannot be trusted to pace a paid
# model call -- several viewers each run their own, and the mutation can simply be
# called in a loop. Enforced here so the bound holds whatever the caller does.
# A user pressing the button is still subject to it; what they override is the
# "nothing here could name anybody" shortcut, not the cost bound.
PASS_COOLDOWN_SECONDS = int(os.environ.get("SPEAKER_NAMING_COOLDOWN_SECONDS", "30"))

DESERIALIZER = TypeDeserializer()

dynamodb = boto3.client("dynamodb")
bedrock = boto3.client(
    service_name="bedrock-runtime",
    region_name=BEDROCK_REGION,
    config=Config(retries={"max_attempts": 50, "mode": "adaptive"}),
)


def _deserialize(item):
    return {key: DESERIALIZER.deserialize(value) for key, value in item.items()}


def read_meeting(call_id):
    """The meeting row, which carries the identity map and the access fields."""
    response = dynamodb.get_item(
        TableName=EVENT_SOURCING_TABLE,
        Key={"PK": {"S": f"c#{call_id}"}, "SK": {"S": f"c#{call_id}"}},
    )
    item = response.get("Item")
    return _deserialize(item) if item else None


def read_segments(call_id):
    """Every final transcript segment for a meeting, across all result pages."""
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
    if pages >= MAX_TRANSCRIPT_PAGES:
        LOGGER.warning(
            "Stopped reading transcript for %s at the %d page limit; it may be incomplete",
            call_id,
            pages,
        )
    return items


def caller_may_edit(meeting, identity):
    """The same rule the read path and the direct-write resolver both apply.

    A Lambda resolver cannot express this as a DynamoDB condition, so it is
    checked here instead: Admin, the meeting's owner, or a user it has been
    shared with. An invocation with no Cognito identity is the backend calling
    itself (the end-of-meeting pass) and is allowed.
    """
    if not identity:
        return True
    username = identity.get("username") or identity.get("sub")
    groups = identity.get("groups") or []
    if "Admin" in groups:
        return True
    if meeting.get("Owner") == username:
        return True
    shared_with = meeting.get("SharedWith")
    if isinstance(shared_with, (list, set)):
        return username in shared_with
    if isinstance(shared_with, str):
        return bool(username) and username in shared_with
    return False


def call_bedrock(prompt):
    """One model call. Temperature 0: this is extraction, not writing."""
    response = bedrock.converse(
        modelId=BEDROCK_MODEL_ID,
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        inferenceConfig={"maxTokens": 1024, "temperature": 0},
    )
    return response["output"]["message"]["content"][0]["text"]


def write_identities(call_id, identities):
    """Store the map through AppSync, so subscribers re-label without polling.

    Writing to DynamoDB directly would be fewer moving parts but would skip the
    `onUpdateCall` publish, and every open meeting page would keep showing voice
    ids until it was reloaded.
    """
    query = """
      mutation UpdateSpeakerIdentities($input: UpdateSpeakerIdentitiesInput!) {
        updateSpeakerIdentities(input: $input) { CallId SpeakerIdentities }
      }
    """
    payload = json.dumps(
        {
            "query": query,
            "variables": {
                "input": {"CallId": call_id, "SpeakerIdentities": json.dumps(identities)}
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
        APPSYNC_GRAPHQL_URL,
        data=payload,
        headers=dict(request.headers),
        timeout=20,
    )
    body = response.json()
    if body.get("errors"):
        raise RuntimeError(f"updateSpeakerIdentities failed: {body['errors']}")
    return body


def resolve(call_id, identity=None, force=False):
    """Run one naming pass. Returns a small result object for the caller."""
    meeting = read_meeting(call_id)
    if meeting is None:
        return {"ok": False, "reason": "no such meeting", "applied": []}
    if not caller_may_edit(meeting, identity):
        return {"ok": False, "reason": "not permitted", "applied": []}

    identities = pure.parse_identities(meeting.get("SpeakerIdentities"))
    turns = pure.select_turns(pure.build_turns(read_segments(call_id)), MAX_TURNS)

    now_iso = datetime.now(timezone.utc).isoformat()
    if pure.within_cooldown(identities, now_iso, PASS_COOLDOWN_SECONDS):
        LOGGER.info(
            "Declining a pass for %s: one ran less than %ds ago", call_id, PASS_COOLDOWN_SECONDS
        )
        return {"ok": True, "reason": "a pass ran very recently", "applied": []}
    if not pure.transcript_has_grown(identities, turns):
        LOGGER.info("Declining a pass for %s: nobody has spoken since the last one", call_id)
        return {"ok": True, "reason": "nothing new said since the last pass", "applied": []}

    if not pure.worth_a_model_call(turns, identities, force=force):
        LOGGER.info("Nothing to name for %s (turns=%d, forced=%s)", call_id, len(turns), force)
        return {"ok": True, "reason": "nothing to name", "applied": []}
    # Stamped whether or not anything is applied below, so a pass that found
    # nothing still holds the cooldown -- otherwise the cheapest way to spend
    # money would be to ask repeatedly about a meeting with no introductions.
    #
    # The cost is that an empty pass still writes, and therefore still publishes
    # onUpdateCall to connected clients. That is one message per pass per client,
    # bounded by the cooldown itself, and the payload is the full Call so nothing
    # on screen changes. Cheap next to an unbounded number of model calls.
    stamp_only = True

    labels = pure.unresolved_voices(turns, identities)
    prompt = pure.build_prompt(turns, labels, identities.get("candidates") or [])
    raw = call_bedrock(prompt)
    proposals = pure.parse_model_response(raw)
    updated, applied = pure.apply_proposals(identities, proposals, turns, now_iso)

    # The gap between proposed and applied is how much the verification
    # rejected, which is the number worth watching when tuning the prompt.
    LOGGER.info(
        "Speaker naming pass for %s: turns=%d open_labels=%d proposed=%d applied=%d",
        call_id,
        len(turns),
        len(labels),
        len(proposals),
        len(applied),
    )
    if applied:
        # Re-read before writing. The model call took seconds, and the write
        # replaces the whole blob with no ordering guard (deliberately, so a
        # human rename always wins), so writing the snapshot taken before the
        # model call would quietly discard a name typed in the meantime.
        latest = pure.parse_identities((read_meeting(call_id) or {}).get("SpeakerIdentities"))
        new_entries = {entry["key"]: updated["identities"][entry["key"]] for entry in applied}
        merged, changed = pure.merge_applied_onto(latest, new_entries)
        if changed:
            write_identities(call_id, pure.stamp_pass(merged, now_iso, len(turns)))
        else:
            LOGGER.info("Every inferred name for %s was superseded while the pass ran", call_id)
        stamp_only = False
    if stamp_only:
        latest = pure.parse_identities((read_meeting(call_id) or {}).get("SpeakerIdentities"))
        write_identities(call_id, pure.stamp_pass(latest, now_iso, len(turns)))
    return {"ok": True, "reason": "ok", "applied": applied}


def handler(event, context):  # pylint: disable=unused-argument
    """Entry point for both the AppSync resolver and the backend invoke."""
    LOGGER.debug("Speaker name resolver event: %s", json.dumps(event, default=str)[:2000])

    # An AppSync Lambda resolver receives the whole context; a backend invoke
    # sends the Kinesis message, which carries CallId at the top level.
    arguments = (event.get("arguments") or {}).get("input") or {}
    call_id = arguments.get("CallId") or event.get("CallId")
    if not call_id:
        raise ValueError("no CallId in event")

    identity = event.get("identity")
    # Only a user pressing the button overrides the "is there anything here
    # worth a model call" shortcut; the automatic passes respect it.
    force = bool(arguments.get("Force")) and identity is not None

    try:
        result = resolve(call_id, identity=identity, force=force)
    except Exception:  # noqa: BLE001 - reported to the caller, see below
        # Naming is an enhancement. The end-of-meeting caller must not have a
        # meeting fail to close because a model call did, and the UI caller gets
        # a message rather than a stack trace.
        # Logged in full; the caller gets a message rather than a service error,
        # which would otherwise put DynamoDB and AppSync internals in a browser.
        LOGGER.exception("Speaker naming pass failed for %s", call_id)
        return {
            "Success": False,
            "Message": "could not identify speakers just now",
            "NamesApplied": 0,
        }

    return {
        "Success": result["ok"],
        "Message": result["reason"],
        "NamesApplied": len(result["applied"]),
    }
