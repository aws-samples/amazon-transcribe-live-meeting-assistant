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
from bedrock_compat import SPEAKER_NAMING_MAX_TOKENS, build_inference_config, extract_text
from boto3.dynamodb.types import TypeDeserializer
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.config import Config

# pylint: disable=import-error
from lma_metering import MeteringWriter, bedrock_service, bedrock_usage  # configuration layer

# pylint: enable=import-error

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
# passes lives in the browser, and several viewers each run their own, so pacing
# cannot be left to the client: this is what stops a watched meeting paying for a
# pass every few seconds. A user pressing the button is subject to it too; what
# the button overrides is the "nothing here could name anybody" shortcut.
#
# It is a guard against accidental repeat spend, not a defence against a
# determined caller. The stamp lives in the meeting's own identity map, which a
# user with write access to the meeting can overwrite, so somebody who sets out
# to loop this can. Bounding that properly needs a rate limit at the API, which
# is a deployment-level control (see the WAF options) rather than something this
# function can enforce.
PASS_COOLDOWN_SECONDS = int(os.environ.get("SPEAKER_NAMING_COOLDOWN_SECONDS", "30"))

DESERIALIZER = TypeDeserializer()

dynamodb = boto3.client("dynamodb")
# Each naming pass is a model call billed to the meeting; the automatic pass runs
# as often as every 45 seconds while somebody has the meeting open, so this is
# usually a meeting's most frequent Bedrock line. Inert when METERING_TABLE_NAME
# is unset.
METERING = MeteringWriter(source="speaker-name-resolver", client=dynamodb)
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
    """The same rule the read path and the direct-write resolver both apply.

    A Lambda resolver cannot express this as a DynamoDB condition, so it is
    checked here instead: Admin, the meeting's owner, or a user it has been
    shared with. An invocation with no Cognito identity is the backend calling
    itself (the end-of-meeting pass) and is allowed.

    `SharedWith` is compared entry by entry in every shape it is stored in --
    the DynamoDB List `shareCall` writes, a string set, and the comma-separated
    or "[a, b]" String on older rows -- as the templates compare it
    (authorizeCallWrite.response.vtl reads the entries for the direct writes).

    One difference from the templates is deliberate: a caller with no
    `username` falls back to `sub` here, where a VTL condition matches on
    `username` alone. An AppSync Cognito caller always carries a username, so
    the fallback is a belt for a shape that does not arrive rather than a
    second way in.
    """
    if not identity:
        return True
    username = identity.get("username") or identity.get("sub")
    groups = identity.get("groups") or []
    if "Admin" in groups:
        return True
    # Only a named caller can match one of the fields below. A meeting row
    # carries no Owner attribute until someone owns it, and absent reads as
    # None, so every comparison needs a name on both sides to mean anything.
    if not username:
        return False
    if meeting.get("Owner") == username:
        return True
    return username in shared_with_entries(meeting.get("SharedWith"))


def call_bedrock(prompt, call_id=None):
    """One model call, metered against ``call_id``.

    Temperature 0 where the model accepts it: this is extraction, not writing, and
    two passes over one meeting should agree. Only the text blocks are read -- the
    output is parsed into an identity map, so a reasoning block reaching the parser
    would corrupt attribution rather than just read oddly.
    """
    response = bedrock.converse(
        modelId=BEDROCK_MODEL_ID,
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        inferenceConfig=build_inference_config(BEDROCK_MODEL_ID, SPEAKER_NAMING_MAX_TOKENS),
    )
    METERING.record(
        call_id,
        "SpeakerNaming",
        bedrock_service(BEDROCK_MODEL_ID),
        bedrock_usage(response.get("usage")),
        region=BEDROCK_REGION if BEDROCK_REGION != os.environ.get("AWS_REGION") else None,
    )
    return extract_text(response)


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


def resolve(call_id, identity=None, force=False, final=False):
    """Run one naming pass. Returns a small result object for the caller."""
    meeting = read_meeting(call_id)
    if meeting is None:
        return {"ok": False, "reason": "no such meeting", "applied": []}
    if not caller_may_edit(meeting, identity):
        return {"ok": False, "reason": "not permitted", "applied": []}

    identities = pure.parse_identities(meeting.get("SpeakerIdentities"))
    all_turns = pure.build_turns(read_segments(call_id))
    # The raw size drives the growth check and the stamp; the trimmed list is
    # only what the model reads. Conflating them silenced naming past the trim
    # limit, because both sides of the comparison saturated at it.
    total_turns = len(all_turns)
    turns = pure.select_turns(all_turns, MAX_TURNS)

    now_iso = datetime.now(timezone.utc).isoformat()
    # The end-of-meeting pass skips both gates. It runs once per meeting, is not
    # driven by a client, and is the pass the summary depends on -- declining it
    # because a browser timer happened to fire 20 seconds earlier would lose the
    # names from the summary for most meetings anybody had open.
    if not final and pure.within_cooldown(identities, now_iso, PASS_COOLDOWN_SECONDS):
        LOGGER.info(
            "Declining a pass for %s: one ran less than %ds ago", call_id, PASS_COOLDOWN_SECONDS
        )
        return {"ok": True, "reason": "a pass ran very recently", "applied": []}
    # `force` means a person pressed the button, and it has to bypass this gate
    # as well as the model shortcut. On an ended meeting the transcript can never
    # grow again, so without the bypass every press after the first pass was
    # declined for ever -- including the documented recovery of pasting an
    # attendee list and asking again, which is exactly when the button matters
    # most. Deleting segments would strand a meeting the same way. The cooldown
    # above still applies, so this is not a way to spend without limit.
    if not final and not force and not pure.transcript_has_grown(identities, total_turns):
        LOGGER.info("Declining a pass for %s: nobody has spoken since the last one", call_id)
        return {"ok": True, "reason": "nothing new said since the last pass", "applied": []}

    # `final` counts as force here too. The end-of-meeting pass is the
    # once-per-meeting authoritative one, and skipping it on a cheap heuristic
    # is exactly the over-rejection this module has been bitten by five times:
    # an introduction with no phrase a regex can spot -- "Sarah Lee, platform
    # engineering" -- would never be found at all.
    if not pure.worth_a_model_call(turns, identities, force=force or final, all_turns=all_turns):
        LOGGER.info("Nothing to name for %s (turns=%d, forced=%s)", call_id, len(turns), force)
        return {"ok": True, "reason": "nothing to name", "applied": []}

    labels = pure.unresolved_voices(turns, identities, all_turns)
    prompt = pure.build_prompt(turns, labels, identities.get("candidates") or [])
    raw = call_bedrock(prompt, call_id)
    proposals = pure.parse_model_response(raw)
    updated, applied = pure.apply_proposals(
        identities, proposals, turns, now_iso, all_turns=all_turns
    )

    # The gap between proposed and applied is how much the verification
    # rejected, which is the number worth watching when tuning the prompt.
    LOGGER.info(
        "Speaker naming pass for %s: turns=%d/%d open_labels=%d proposed=%d applied=%d",
        call_id,
        len(turns),
        total_turns,
        len(labels),
        len(proposals),
        len(applied),
    )
    # Re-read before writing. The model call took seconds, and the write replaces
    # the whole blob with no ordering guard (deliberately, so a human rename
    # always wins), so writing the snapshot taken before the model call would
    # quietly discard a name typed in the meantime.
    latest = pure.parse_identities((read_meeting(call_id) or {}).get("SpeakerIdentities"))
    new_entries = {entry["key"]: updated["identities"][entry["key"]] for entry in applied}
    merged, changed = pure.merge_applied_onto(latest, new_entries)
    if applied and not changed:
        LOGGER.info("Every inferred name for %s was superseded while the pass ran", call_id)
    # Stamped unconditionally, including when nothing survived the merge: this
    # pass cost a model call either way, and leaving no stamp would let the next
    # request pay for another immediately.
    write_identities(call_id, pure.stamp_pass(merged, now_iso, total_turns))
    return {"ok": True, "reason": "ok", "applied": applied}


def handler(event, context):  # pylint: disable=unused-argument
    """Entry point for both the AppSync resolver and the backend invoke."""
    # The AppSync resolver sends the mutation's arguments and the caller's
    # identity; a backend invoke sends the Kinesis message, which carries CallId
    # at the top level.
    arguments = (event.get("arguments") or {}).get("input") or {}
    call_id = arguments.get("CallId") or event.get("CallId")
    if not call_id:
        raise ValueError("no CallId in event")

    # Which meeting and which leg: the two fields of the event this function acts
    # on, named rather than serialised, so the log line says what ran.
    LOGGER.debug("Speaker name resolver event for %s, trigger %s", call_id, event.get("Trigger"))

    identity = event.get("identity")
    # The backend's end-of-meeting invoke says so, and only the backend can: a
    # browser caller always arrives with an identity.
    final = event.get("Trigger") == "meeting-end" and identity is None
    # Only a user pressing the button overrides the "is there anything here
    # worth a model call" shortcut; the automatic passes respect it.
    force = bool(arguments.get("Force")) and identity is not None

    try:
        result = resolve(call_id, identity=identity, force=force, final=final)
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
