#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Live guidance: read a meeting in progress and offer guidance to the people running it.

Reached two ways:

* **A run** -- invoked asynchronously by the call event processor, at most once
  every ``intervalSeconds`` per execution environment, for a finished speech
  segment of a meeting whose ``GuidanceEnabled`` is true. The run claims the
  meeting's state row (``gd#<CallId>`` / ``analyzer``) with one conditional
  write, which holds only when no other run holds it, the last run was at least
  ``intervalSeconds`` ago and the meeting has not used ``maxRunsPerMeeting``
  runs; so of the several invocations a busy stream produces, one reaches the
  model and the others stop after that write. A run with fewer than
  ``minNewSegments`` new segments releases the row and calls no model. A failed
  run is logged and counted, and the next one waits a full interval: nothing is
  retried (the function's asynchronous retries are off too).
* **A digest on request** -- the meeting page's "Digest so far" button and the
  MCP tool ``request_meeting_digest``, through the ``requestMeetingDigest``
  AppSync mutation. The caller's own identity is checked here, as the agenda
  deriver checks it.

Checklist changes are written through ``updateCallChecklistItem`` with
``StatusSource: assistant``, so open meeting pages update and a status a user
set is never changed; guidance is published with
``lma_notifications.publish_meeting_notification``, category ``guidance``. The
state row is deleted with the meeting and with its transcript, by the functions
that delete ``gd#<CallId>``. See pure.py for the judgement and
docs/live-guidance.md.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Mapping, Optional, Tuple

import boto3
import pure
from bedrock_compat import (
    PROMPT_CACHE_MESSAGES,
    PROMPT_CACHE_SYSTEM,
    PROMPT_CACHE_TOOLS,
    additional_request_fields,
    build_inference_config,
    prompt_cache_sections,
)
from boto3.dynamodb.types import TypeDeserializer
from botocore.config import Config
from botocore.exceptions import ClientError

# pylint: disable=import-error
from lma_config import LANGUAGE_MODELS, MEETING_ASSIST, read_setting, resolve_model_id
from lma_metering import MeteringWriter, bedrock_service, bedrock_usage
from lma_notifications import (
    GUIDANCE,
    NotificationError,
    NotificationRateLimited,
    NotificationTranscriptDeleted,
    appsync_graphql,
    publish_meeting_notification,
    transcript_expires_after,
)

# pylint: enable=import-error

# Standard logging rather than Powertools, as in the agenda deriver: the layer
# that carries Powertools is built for arm64 only.
LOGGER = logging.getLogger()
LOGGER.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

#: The model when the configuration cannot be read; the Meeting Assistant
#: model's shipped default in setting_defaults.json.
FALLBACK_MODEL_ID = "global.anthropic.claude-haiku-5-5"

EVENT_SOURCING_TABLE = os.environ["EVENT_SOURCING_TABLE"]
BEDROCK_REGION = os.environ.get("BEDROCK_REGION_OVERRIDE") or os.environ["AWS_REGION"]

#: The metering context the meeting cost estimate lists live guidance under.
METERING_CONTEXT = "LiveGuidance"
#: How long a claim holds the state row: longer than the function's timeout, so
#: a run that is killed cannot keep the meeting locked for more than this.
LEASE_SECONDS = int(os.environ.get("LEASE_SECONDS", "180"))
RUN_MAX_TOKENS = 3000
DIGEST_MAX_TOKENS = 1500
#: The least time between two digests of one meeting.
DIGEST_INTERVAL_SECONDS = 60
MAX_TRANSCRIPT_PAGES = 100
#: Guidance other publishers left on the meeting, shown to the model so it does
#: not repeat them.
MAX_OTHER_NOTES = 5

STATE_SK = "analyzer"

# The same full Call field set onUpdateCall selects (agenda_deriver's copy).
# AppSync publishes a mutation's own response to that subscription, and a field
# it selects that the mutation did not arrives as null in every open page.
_CALL_FIELDS = """
      PK
      SK
      CallId
      AgentId
      CallSummaryText
      CallSummaryState
      DetectedLanguages
      TranscriptionPause
      SpeakerIdentities
      Agenda
      AgendaSource
      AgendaUpdatedBy
      AgendaUpdatedAt
      Labels {
        Key
        Value
      }
      Audience
      MeetingMetadataUpdatedAt
      MeetingContext
      MeetingContextUpdatedBy
      MeetingContextUpdatedAt
      Checklist {
        ItemId
        Text
        Kind
        Priority
        Source
        Status
        StatusSource
        Evidence {
          SegmentId
          StartTime
          Quote
        }
        Note
        UpdatedBy
        UpdatedAt
      }
      ChecklistUpdatedAt
      SummaryProfile
      GuidanceEnabled
      ActionItems {
        Text
        Owner
        DueDate
        DueText
        StartTime
        SegmentId
      }
      MeetingPlatform
      Status
      UpdatedAt
      CreatedAt
      CustomerPhoneNumber
      SystemPhoneNumber
      RecordingUrl
      VideoRecordingUrl
      RecordingUrls
      RecordingUrlSessions
      VideoRecordingUrls
      VideoRecordingUrlSessions
      SessionNumber
      SessionOffsetSeconds
      SessionStartedNumber
      Sessions { SessionNumber OffsetSeconds StartedAt }
      Owner
      SharedWith
      TotalConversationDurationMillis
      Sentiment {
        OverallSentiment { REMOTE LOCAL AGENT CALLER }
        SentimentByPeriod {
          QUARTER {
            REMOTE { BeginOffsetMillis EndOffsetMillis Score }
            LOCAL { BeginOffsetMillis EndOffsetMillis Score }
            AGENT { BeginOffsetMillis EndOffsetMillis Score }
            CALLER { BeginOffsetMillis EndOffsetMillis Score }
          }
        }
      }
"""

UPDATE_CHECKLIST_ITEM = (
    "mutation UpdateCallChecklistItem($input: UpdateCallChecklistItemInput!) {\n"
    "  updateCallChecklistItem(input: $input) {" + _CALL_FIELDS + "  }\n}"
)

DESERIALIZER = TypeDeserializer()

dynamodb = boto3.client("dynamodb")
# Every model call is billed to its meeting. Inert when METERING_TABLE_NAME is unset.
METERING = MeteringWriter(source="live-guidance", client=dynamodb)
bedrock = boto3.client(
    service_name="bedrock-runtime",
    region_name=BEDROCK_REGION,
    config=Config(retries={"max_attempts": 50, "mode": "adaptive"}),
)


class DigestError(Exception):
    """A digest that could not be made, with the message the caller is shown."""


# ── reading ──────────────────────────────────────────────────────────────────


def _deserialize(item):
    return {key: DESERIALIZER.deserialize(value) for key, value in item.items()}


def _plain(value: Any) -> Any:
    """DynamoDB numbers as floats, for the pure code and for JSON."""
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, Decimal):
        return float(value)
    return value


def _meeting_key(call_id):
    return {"PK": {"S": f"c#{call_id}"}, "SK": {"S": f"c#{call_id}"}}


def _state_key(call_id):
    return {"PK": {"S": f"gd#{call_id}"}, "SK": {"S": STATE_SK}}


def read_meeting(call_id: str) -> Optional[Dict[str, Any]]:
    item = dynamodb.get_item(TableName=EVENT_SOURCING_TABLE, Key=_meeting_key(call_id)).get("Item")
    return _plain(_deserialize(item)) if item else None


def read_state(call_id: str) -> Dict[str, Any]:
    item = dynamodb.get_item(TableName=EVENT_SOURCING_TABLE, Key=_state_key(call_id)).get("Item")
    return _plain(_deserialize(item)) if item else {}


def _query(pk: str, **extra: Any) -> List[Dict[str, Any]]:
    """The items of one partition, across result pages (one page when ``Limit`` is given)."""
    items: List[Dict[str, Any]] = []
    start_key = None
    for _page in range(MAX_TRANSCRIPT_PAGES):
        kwargs = {
            "TableName": EVENT_SOURCING_TABLE,
            "KeyConditionExpression": "PK = :pk",
            "ExpressionAttributeValues": {":pk": {"S": pk}},
            **extra,
        }
        if start_key:
            kwargs["ExclusiveStartKey"] = start_key
        response = dynamodb.query(**kwargs)
        items.extend(_plain(_deserialize(item)) for item in response.get("Items", []))
        next_key = response.get("LastEvaluatedKey")
        if not next_key or next_key == start_key or extra.get("Limit"):
            break
        start_key = next_key
    return items


def read_segments(call_id: str) -> List[Dict[str, Any]]:
    """Every transcript segment of the meeting."""
    return _query(f"trs#{call_id}")


def other_guidance(call_id: str) -> List[str]:
    """The newest guidance other publishers (an MCP client) left on the meeting."""
    rows = _query(
        f"nt#{call_id}",
        KeyConditionExpression="PK = :pk AND begins_with(SK, :n)",
        ExpressionAttributeValues={":pk": {"S": f"nt#{call_id}"}, ":n": {"S": "n#"}},
        ScanIndexForward=False,
        Limit=50,
    )
    notes = []
    for row in rows:
        if row.get("Category") != GUIDANCE or row.get("Source") == pure.SOURCE:
            continue
        text = " ".join(str(row.get("Title") or row.get("Body") or "").split())
        if text:
            notes.append(text[:200])
        if len(notes) >= MAX_OTHER_NOTES:
            break
    return notes


def load_settings() -> pure.Settings:
    return pure.Settings.from_raw(read_setting(MEETING_ASSIST.name, "guidance", default={}))


def model_for(settings: pure.Settings) -> str:
    """The setting's model, else the Meeting Assistant model, in the inference geography."""
    if settings.model_id:
        return resolve_model_id(settings.model_id, region=BEDROCK_REGION)
    return resolve_model_id(
        read_setting(LANGUAGE_MODELS.name, "meetingAssistModelId", default=FALLBACK_MODEL_ID),
        region=BEDROCK_REGION,
    )


# ── the state row ────────────────────────────────────────────────────────────


def _conflict(error: ClientError) -> bool:
    return error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


def _expiry(meeting: Mapping[str, Any]) -> Optional[int]:
    """The state row's expiry: the transcript's, since its summary is drawn from it."""
    expires = transcript_expires_after()
    meeting_expiry = meeting.get("ExpiresAfter")
    if isinstance(meeting_expiry, (int, float)) and meeting_expiry > 0:
        expires = min(expires, int(meeting_expiry)) if expires else int(meeting_expiry)
    return expires


def claim(
    call_id: str, settings: pure.Settings, now: float, expires: Optional[int]
) -> Optional[Dict[str, Any]]:
    """Take the meeting's run lease; the state row, or None when a run may not start now."""
    values = {
        ":lease": {"N": str(int(now) + LEASE_SECONDS)},
        ":now": {"N": str(now)},
        ":due": {"N": str(now - settings.interval_seconds)},
        ":max": {"N": str(settings.max_runs_per_meeting)},
        ":call": {"S": call_id},
    }
    sets = ["LeaseUntil = :lease", "CallId = :call"]
    if expires:
        values[":ttl"] = {"N": str(expires)}
        sets.append("ExpiresAfter = if_not_exists(ExpiresAfter, :ttl)")
    try:
        response = dynamodb.update_item(
            TableName=EVENT_SOURCING_TABLE,
            Key=_state_key(call_id),
            UpdateExpression="SET " + ", ".join(sets),
            ConditionExpression=(
                "(attribute_not_exists(LeaseUntil) OR LeaseUntil < :now) "
                "AND (attribute_not_exists(LastRunAt) OR LastRunAt <= :due) "
                "AND (attribute_not_exists(RunCount) OR RunCount < :max)"
            ),
            ExpressionAttributeValues=values,
            ReturnValues="ALL_NEW",
        )
    except ClientError as error:
        if _conflict(error):
            return None
        raise
    return _plain(_deserialize(response.get("Attributes") or {}))


def claim_digest(call_id: str, now: float, expires: Optional[int]) -> bool:
    """Record a digest on the state row; False when the meeting had one in the last minute.

    The bound on digests, which a person or an MCP client asks for: each is a
    model call, so one meeting gets at most one a minute, held by one
    conditional write as the runs' interval is.
    """
    values: Dict[str, Any] = {
        ":now": {"N": str(now)},
        ":due": {"N": str(now - DIGEST_INTERVAL_SECONDS)},
        ":call": {"S": call_id},
    }
    sets = ["LastDigestAt = :now", "CallId = :call"]
    if expires:
        values[":ttl"] = {"N": str(expires)}
        sets.append("ExpiresAfter = if_not_exists(ExpiresAfter, :ttl)")
    try:
        dynamodb.update_item(
            TableName=EVENT_SOURCING_TABLE,
            Key=_state_key(call_id),
            UpdateExpression="SET " + ", ".join(sets),
            ConditionExpression="attribute_not_exists(LastDigestAt) OR LastDigestAt <= :due",
            ExpressionAttributeValues=values,
        )
    except ClientError as error:
        if _conflict(error):
            return False
        raise
    return True


def release(call_id: str, lease: Any, ran: bool, now: float, **fields: Any) -> None:
    """Give the lease back; after a run (``ran``), count it and store what it leaves.

    Only this run's lease is released: the condition names it, so a run that
    outlived its lease cannot clear another's.
    """
    sets: List[str] = []
    values: Dict[str, Any] = {":lease": {"N": str(int(lease))}}
    names: Dict[str, str] = {}
    if ran:
        sets += ["LastRunAt = :now", "RunCount = if_not_exists(RunCount, :zero) + :one"]
        values.update({":now": {"N": str(now)}, ":zero": {"N": "0"}, ":one": {"N": "1"}})
    for number, (name, value) in enumerate(fields.items()):
        names[f"#f{number}"] = name
        values[f":f{number}"] = (
            {"N": str(value)} if isinstance(value, (int, float)) else {"S": str(value)}
        )
        sets.append(f"#f{number} = :f{number}")
    expression = ("SET " + ", ".join(sets) + " " if sets else "") + "REMOVE LeaseUntil"
    kwargs: Dict[str, Any] = {
        "TableName": EVENT_SOURCING_TABLE,
        "Key": _state_key(call_id),
        "UpdateExpression": expression,
        "ConditionExpression": "LeaseUntil = :lease",
        "ExpressionAttributeValues": values,
    }
    if names:
        kwargs["ExpressionAttributeNames"] = names
    try:
        dynamodb.update_item(**kwargs)
    except ClientError as error:
        if not _conflict(error):
            raise
        LOGGER.warning("The guidance lease for %s was no longer this run's", call_id)


# ── the model ────────────────────────────────────────────────────────────────


def _with_cache_points(model_id, system, messages, tool) -> Tuple[list, list, list]:
    """The request with cache points after the parts that repeat, where the model caches."""
    caching = read_setting(MEETING_ASSIST.name, "promptCachingEnabled", default=True)
    sections = prompt_cache_sections(model_id) if caching else frozenset()
    point = {"cachePoint": {"type": "default"}}
    tools = [tool] + ([point] if PROMPT_CACHE_TOOLS in sections else [])
    if PROMPT_CACHE_SYSTEM in sections:
        system = system + [point]
    if PROMPT_CACHE_MESSAGES in sections and len(messages[0]["content"]) > 1:
        content = messages[0]["content"]
        messages = [{"role": "user", "content": [content[0], point, *content[1:]]}]
    return system, messages, tools


def call_model(  # pylint: disable=too-many-arguments
    call_id: str, model_id: str, system: list, messages: list, tool: dict, max_tokens: int
) -> Optional[Dict[str, Any]]:
    """One model call, metered against the meeting; the tool's input, or None."""
    system, messages, tools = _with_cache_points(model_id, system, messages, tool)
    request: Dict[str, Any] = {
        "modelId": model_id,
        "system": system,
        "messages": messages,
        # "auto" rather than naming the tool: the Claude 5 models refuse a forced
        # tool choice. The instructions ask for the tool, and an answer given as
        # JSON text instead is read the same way (pure.tool_input).
        "toolConfig": {"tools": tools, "toolChoice": {"auto": {}}},
        "inferenceConfig": build_inference_config(model_id, max_tokens),
    }
    # A low reasoning effort where the model takes one: this is a short, frequent
    # call, and reasoning is billed as output.
    extra = additional_request_fields(model_id, "low")
    if extra:
        request["additionalModelRequestFields"] = extra
    response = bedrock.converse(**request)
    usage = response.get("usage") or {}
    METERING.record(
        call_id,
        METERING_CONTEXT,
        bedrock_service(model_id),
        bedrock_usage(usage),
        region=BEDROCK_REGION if BEDROCK_REGION != os.environ.get("AWS_REGION") else None,
    )
    # Sizes only, never content: the transcript and the guidance are meeting content.
    LOGGER.info(
        "Live guidance model call for %s: input=%s output=%s cacheRead=%s stop=%s",
        call_id,
        usage.get("inputTokens"),
        usage.get("outputTokens"),
        usage.get("cacheReadInputTokens"),
        response.get("stopReason"),
    )
    return pure.tool_input(response, tool["toolSpec"]["name"])


# ── writing ──────────────────────────────────────────────────────────────────


def write_change(call_id: str, change: pure.ChecklistChange) -> bool:
    """One checklist change through AppSync; False when the API refused it."""
    try:
        appsync_graphql(
            UPDATE_CHECKLIST_ITEM,
            {
                "input": {
                    "CallId": call_id,
                    "ItemId": change.item_id,
                    "Status": change.status,
                    "AddEvidence": change.evidence,
                    "StatusSource": "assistant",
                    "UpdatedBy": pure.UPDATED_BY,
                }
            },
        )
    except NotificationError as error:
        # StatusSetByUser when a user decided the item meanwhile, or a conflict
        # with a concurrent change: either way the other change stands.
        LOGGER.info(
            "Checklist item %s of %s not changed: %s", change.item_id, call_id, error.error_type
        )
        return False
    return True


def publish(call_id: str, candidate: pure.Candidate) -> None:
    publish_meeting_notification(
        call_id,
        category=GUIDANCE,
        type=candidate.type,
        title=candidate.title,
        body=candidate.body or None,
        severity=candidate.severity,
        refs=candidate.refs(),
        dedup_key=candidate.dedup_key,
        actions=candidate.actions() or None,
        source=pure.SOURCE,
    )


def withdraw(call_id: str, key: str, entry: Mapping[str, Any]) -> None:
    """Take a published item off the page: the same item again, expiring now."""
    publish_meeting_notification(
        call_id,
        category=GUIDANCE,
        type=str(entry.get("type")),
        title=entry.get("title"),
        body=entry.get("body") or None,
        severity=entry.get("severity") or "suggestion",
        refs=entry.get("refs") or None,
        dedup_key=key,
        expires_at=datetime.now(timezone.utc),
        source=pure.SOURCE,
    )


# ── a run ────────────────────────────────────────────────────────────────────


def _json_field(state: Mapping[str, Any], name: str, default: Any) -> Any:
    try:
        value = json.loads(state.get(name) or "null")
    except (TypeError, ValueError):
        return default
    return value if isinstance(value, type(default)) else default


def why_not(meeting: Optional[Mapping[str, Any]], settings: pure.Settings) -> Optional[str]:
    """Why a meeting is not analysed now, or None when it is."""
    if not settings.available:
        return "live guidance is turned off for this deployment"
    if meeting is None:
        return "no such meeting"
    if meeting.get("GuidanceEnabled") is not True:
        return "live guidance is off for this meeting"
    if meeting.get("Status") == "ENDED":
        return "the meeting has ended"
    if meeting.get("TranscriptDeletedAt"):
        return "the meeting's transcript is deleted"
    return None


def run(call_id: str) -> Dict[str, Any]:
    """One automatic run. Returns a small result for the log and the tests."""
    settings = load_settings()
    meeting = read_meeting(call_id)
    reason = why_not(meeting, settings)
    if reason or meeting is None:
        return {"ran": False, "reason": reason}
    now = time.time()
    state = claim(call_id, settings, now, _expiry(meeting))
    if state is None:
        return {"ran": False, "reason": "another run holds the meeting, or one ran recently"}
    lease = state["LeaseUntil"]
    try:
        return _analyse(call_id, meeting, settings, state, now)
    except Exception:  # pylint: disable=broad-except
        LOGGER.exception("Live guidance run failed for %s; the next one waits an interval", call_id)
        release(call_id, lease, ran=True, now=now)
        return {"ran": False, "reason": "the run failed"}


def _analyse(  # pylint: disable=too-many-arguments,too-many-locals
    call_id: str,
    meeting: Mapping[str, Any],
    settings: pure.Settings,
    state: Mapping[str, Any],
    now: float,
) -> Dict[str, Any]:
    lease = state["LeaseUntil"]
    segments = pure.final_segments(
        read_segments(call_id), meeting.get("SpeakerIdentities"), meeting.get("CustomerPhoneNumber")
    )
    fresh = pure.new_segments(segments, state.get("LastSegmentEndTime"))
    if len(fresh) < settings.min_new_segments:
        release(call_id, lease, ran=False, now=now)
        return {"ran": False, "reason": f"{len(fresh)} new segments"}

    published: Dict[str, Dict[str, Any]] = _json_field(state, "Published", {})
    checklist = pure.checklist_items(meeting.get("Checklist"))
    transcript, names = pure.transcript_block(fresh)
    model_id = model_for(settings)
    system, messages = pure.build_request(
        meeting,
        settings,
        str(state.get("RollingSummary") or ""),
        transcript,
        max(s.end for s in segments),
        pure.render_shown(published, other_guidance(call_id)),
    )
    answer = call_model(call_id, model_id, system, messages, pure.REPORT_TOOL, RUN_MAX_TOKENS)
    analysis = pure.parse_analysis(answer, checklist, names, settings.confidence_threshold)
    if analysis is None:
        LOGGER.warning("Live guidance answer for %s was malformed; dropped", call_id)
        release(call_id, lease, ran=True, now=now)
        return {"ran": True, "reason": "malformed answer", "changed": 0, "published": 0}

    by_id = {item.get("ItemId"): item for item in checklist}
    changed = 0
    for change in analysis.changes:
        if write_change(call_id, change):
            changed += 1
            by_id[change.item_id] = {**by_id[change.item_id], "Status": change.status}
    checklist = list(by_id.values())

    still_open = {
        item.get("ItemId")
        for item in checklist
        if (item.get("Status") or "open") in pure.ELIGIBLE_STATUSES
    }
    candidates = [
        c
        for c in analysis.candidates
        if not (c.item_id and c.type in pure.ITEM_BOUND_TYPES and c.item_id not in still_open)
    ]
    recent = pure.recent_publishes(_json_field(state, "PublishTimes", []), now)
    chosen = pure.select(candidates, published, recent, settings.max_items_per_10_min)
    published, count = _publish_chosen(call_id, chosen, published, recent, now)
    for key in pure.withdrawals(published, checklist):
        try:
            withdraw(call_id, key, published[key])
        except NotificationError as error:
            LOGGER.info("Guidance item for %s not withdrawn: %s", call_id, error.error_type)
            continue
        published[key]["withdrawn"] = True

    release(
        call_id,
        lease,
        ran=True,
        now=now,
        LastSegmentEndTime=max(s.end for s in fresh),
        RollingSummary=analysis.summary or str(state.get("RollingSummary") or ""),
        Published=json.dumps(published, ensure_ascii=False),
        PublishTimes=json.dumps(recent),
    )
    LOGGER.info(
        "Live guidance run for %s: segments=%d changes=%d/%d published=%d dropped=%d",
        call_id,
        len(fresh),
        changed,
        len(analysis.changes),
        count,
        analysis.dropped,
    )
    return {"ran": True, "changed": changed, "published": count, "dropped": analysis.dropped}


def _publish_chosen(
    call_id: str,
    chosen: List[pure.Candidate],
    published: Dict[str, Dict[str, Any]],
    recent: List[float],
    now: float,
) -> Tuple[Dict[str, Dict[str, Any]], int]:
    """Publish what this run chose; the updated record and how many were published.

    ``recent`` gains this run's publish times, for the cap's next window.
    """
    count = 0
    for candidate in chosen:
        try:
            publish(call_id, candidate)
        except (NotificationRateLimited, NotificationTranscriptDeleted) as error:
            LOGGER.info("Guidance publish for %s stopped: %s", call_id, error.error_type)
            break
        except NotificationError as error:
            LOGGER.warning("Guidance item for %s refused: %s", call_id, error.error_type)
            continue
        count += 1
        recent.append(now)
        published = pure.remember(published, candidate, now)
    return published, count


# ── a digest ─────────────────────────────────────────────────────────────────


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


def caller_of(identity: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """Who is asking, as meeting_preparation_resolver's caller_of reads the identity.

    Only an IAM identity of an assumed role with no user-pool claims is the
    backend: the MCP server, which checks its own caller first. An empty or
    absent identity has no username and is refused. (A run, which the call
    event processor invokes directly, never reaches this: see handler.)
    """
    identity = identity or {}
    claims = identity.get("claims") or {}
    groups = claims.get("cognito:groups") or identity.get("groups") or []
    if isinstance(groups, str):
        groups = [g.strip() for g in groups.split(",") if g.strip()]
    is_backend = not claims and ":assumed-role/" in str(identity.get("userArn") or "")
    return {
        "username": "" if is_backend else str(identity.get("username") or ""),
        "is_admin": not is_backend and "Admin" in groups,
        "is_backend": is_backend,
    }


def caller_may_edit(meeting: Mapping[str, Any], caller: Mapping[str, Any]) -> bool:
    """The rule of the meeting's other edits: Admin, its owner, or a user it is shared with."""
    if caller["is_backend"] or caller["is_admin"]:
        return True
    username = caller["username"]
    if not username:
        return False
    return meeting.get("Owner") == username or username in shared_with_entries(
        meeting.get("SharedWith")
    )


def digest(call_id: str, identity: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """A digest of the meeting so far, published as a ``meeting_digest`` item."""
    settings = load_settings()
    if not settings.available:
        raise DigestError("Live guidance is turned off for this deployment.")
    caller = caller_of(identity)
    meeting = read_meeting(call_id)
    if meeting is None or not caller_may_edit(meeting, caller):
        raise DigestError("No such meeting, or you may not change it.")
    if meeting.get("TranscriptDeletedAt"):
        raise DigestError("This meeting's transcript is deleted, so there is nothing to digest.")
    segments = pure.final_segments(
        read_segments(call_id), meeting.get("SpeakerIdentities"), meeting.get("CustomerPhoneNumber")
    )
    if not segments:
        raise DigestError("Nothing has been said in the meeting yet.")
    if not claim_digest(call_id, time.time(), _expiry(meeting)):
        raise DigestError(
            "A digest of this meeting was made less than a minute ago. Try again in a minute."
        )
    transcript, _names = pure.transcript_block(segments)
    elapsed = max(s.end for s in segments)
    system, messages = pure.build_digest_request(
        meeting, str(read_state(call_id).get("RollingSummary") or ""), transcript, elapsed
    )
    answer = call_model(
        call_id, model_for(settings), system, messages, pure.DIGEST_TOOL, DIGEST_MAX_TOKENS
    )
    text = pure.parse_digest(answer)
    if not text:
        LOGGER.warning("Digest answer for %s was malformed; dropped", call_id)
        raise DigestError("The digest could not be made just now. Try again in a minute.")
    minute = datetime.now(timezone.utc).strftime("%Y%m%d%H%M")
    user = caller["username"] or None
    try:
        stored = publish_meeting_notification(
            call_id,
            category=GUIDANCE,
            type=pure.DIGEST_TYPE,
            title=f"Digest so far ({pure.format_elapsed(elapsed)})",
            body=text,
            severity="info",
            dedup_key=f"{pure.DIGEST_TYPE}:{minute}",
            source=pure.SOURCE,
            created_by=user,
        )
    except NotificationError as error:
        LOGGER.info("Digest for %s not published: %s", call_id, error.error_type)
        raise DigestError("The digest was made but could not be shown on the meeting.") from error
    return {"Text": text, "NotificationId": stored.get("NotificationId")}


def handler(event, context):  # pylint: disable=unused-argument
    """Entry point for the AppSync resolver (a digest) and the backend invoke (a run)."""
    arguments = event.get("arguments") or {}
    call_id = arguments.get("CallId") or event.get("CallId")
    if not call_id or not isinstance(call_id, str):
        raise ValueError("no CallId in event")
    identity = event.get("identity")
    # Only the backend asks for a run: a browser caller arrives with an identity,
    # and its request is a digest.
    if identity is None and event.get("Trigger") == "auto":
        result = run(call_id)
        LOGGER.info("Live guidance for %s: %s", call_id, result.get("reason") or "ran")
        return result
    try:
        result = digest(call_id, identity)
    except DigestError as error:
        return {"Success": False, "Message": str(error), "Text": None, "NotificationId": None}
    except Exception:  # noqa: BLE001 - reported to the caller as a message
        LOGGER.exception("Digest failed for %s", call_id)
        return {
            "Success": False,
            "Message": "The digest could not be made just now. Try again in a minute.",
            "Text": None,
            "NotificationId": None,
        }
    return {"Success": True, "Message": "Published to the meeting's notifications.", **result}
