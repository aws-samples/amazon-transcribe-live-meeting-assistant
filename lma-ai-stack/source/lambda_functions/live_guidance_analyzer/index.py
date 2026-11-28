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

With fact checks or lookups on (checks.py), a run also reports candidates to
research. They join a queue on the row ``gd#<CallId>`` / ``research``; after the
run gives its lease back, the same invocation researches the queue in batches
under a second lease on that row, which bounds research runs per meeting as the
first bounds runs, for as long as the invocation has time. What is left is
researched after the next run, or by the run at the meeting's end
(``Trigger: meeting-ended``). It invokes the meeting assistant function with ``action:
guidance_research``, which runs one read-only agent over the batch and returns
what the sources say, and publishes what checks.publication lets through, within
the same 10-minute cap as the rest of the guidance.

Checklist changes are written through ``updateCallChecklistItem`` with
``StatusSource: assistant``, so open meeting pages update and a status a user
set is never changed; guidance is published with
``lma_notifications.publish_meeting_notification``, category ``guidance``. The
state row is deleted with the meeting and with its transcript, by the functions
that delete ``gd#<CallId>``. See pure.py for the judgement and
docs/live-guidance.md.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import boto3
import checks
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
from lma_live_pass import LivePass
from lma_live_pass import segments as live_pass
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
#: The research lease's row, beside the run's: ``gd#<CallId>`` / ``research``.
RESEARCH_SK = "research"
#: The least time between two research runs on one meeting, except those that
#: follow one another in one invocation to drain the queue.
RESEARCH_INTERVAL_SECONDS = 20
#: Seconds a research batch is allowed beyond its own deadline (the answer's
#: transfer and the publishing), when deciding whether one more fits.
BATCH_SLACK_SECONDS = 60
#: An invocation's time when the context does not say (the tests).
DEFAULT_BUDGET_SECONDS = 280.0
#: Research answers for which nothing was searched: their candidates are logged
#: as dropped, not counted as checked.
UNSEARCHED = ("no sources", "not a research function")
#: The call event processor's trigger for the run at the meeting's end.
END_TRIGGER = "meeting-ended"
#: How long a research request with no source the deployment can offer keeps
#: runs from asking for candidates, so a deployment with no source pays for none.
NO_SOURCES_PAUSE_SECONDS = 900
#: The meeting assistant function, which runs guidance research. Empty on a
#: deployment without it, where no candidate is asked for.
_CONFIGURED_AGENT_ASSIST = os.environ.get("MEETING_ASSIST_FUNCTION_ARN", "")
#: LMA's own Strands meeting assistant is named STRANDS-<stack>-MeetingAssist;
#: an Agent Assist function of any other name is never sent research requests,
#: which carry meeting excerpts.
_STRANDS_ASSISTANT = re.compile(r":function:STRANDS-[A-Za-z0-9-]+-MeetingAssist$")
MEETING_ASSIST_FUNCTION_ARN = (
    _CONFIGURED_AGENT_ASSIST if _STRANDS_ASSISTANT.search(_CONFIGURED_AGENT_ASSIST) else ""
)

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
      WhiteboardsEnabled
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


class ResearchError(Exception):
    """A research request the meeting assistant function did not answer."""


_lambda_clients: Dict[int, Any] = {}


def _lambda_client(read_timeout: int) -> Any:
    """A Lambda client that waits ``read_timeout`` seconds for an answer and never retries."""
    if read_timeout not in _lambda_clients:
        _lambda_clients[read_timeout] = boto3.client(
            "lambda",
            config=Config(
                read_timeout=read_timeout, connect_timeout=5, retries={"max_attempts": 0}
            ),
        )
    return _lambda_clients[read_timeout]


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


def dismissed_ids(call_id: str) -> List[str]:
    """The NotificationIds anyone dismissed on the meeting (each user's view row, ``u#<user>``).

    Only people who can see the meeting's notifications dismiss them: its
    owner, the users it is shared with, and Admins.
    """
    rows = _query(
        f"nt#{call_id}",
        KeyConditionExpression="PK = :pk AND begins_with(SK, :u)",
        ExpressionAttributeValues={":pk": {"S": f"nt#{call_id}"}, ":u": {"S": "u#"}},
    )
    ids: List[str] = []
    for row in rows:
        values = row.get("Dismissed")
        for value in values if isinstance(values, (list, set, tuple)) else []:
            if isinstance(value, str):
                ids.append(value)
    return ids


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


def _lease() -> LivePass:
    """The run lease on the state row (lma_live_pass), on the module's client."""
    return LivePass(
        dynamodb,
        EVENT_SOURCING_TABLE,
        prefix="gd#",
        sort_key=STATE_SK,
        lease_seconds=LEASE_SECONDS,
        name="guidance",
    )


def claim(
    call_id: str, settings: pure.Settings, now: float, expires: Optional[int]
) -> Optional[Dict[str, Any]]:
    """Take the meeting's run lease; the state row, or None when a run may not start now."""
    return _lease().claim(
        call_id,
        now=now,
        interval_seconds=settings.interval_seconds,
        max_runs=settings.max_runs_per_meeting,
        expires=expires,
    )


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


def _research_lease() -> LivePass:
    """The research lease, on its own row, so research never holds up a run."""
    return LivePass(
        dynamodb,
        EVENT_SOURCING_TABLE,
        prefix="gd#",
        sort_key=RESEARCH_SK,
        lease_seconds=LEASE_SECONDS,
        name="guidance research",
    )


def release(call_id: str, lease: Any, ran: bool, now: float, **fields: Any) -> None:
    """Give the lease back; after a run (``ran``), count it and store what it leaves.

    Only this run's lease is released: the condition names it, so a run that
    outlived its lease cannot clear another's.
    """
    _lease().release(call_id, lease, ran=ran, now=now, **fields)


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


def why_not(
    meeting: Optional[Mapping[str, Any]], settings: pure.Settings, allow_ended: bool = False
) -> Optional[str]:
    """Why a meeting is not analysed now, or None when it is.

    ``allow_ended``: for the research run at the meeting's end, which publishes
    what the meeting's last minutes raised.
    """
    if not settings.available:
        return "live guidance is turned off for this deployment"
    if meeting is None:
        return "no such meeting"
    if meeting.get("GuidanceEnabled") is not True:
        return "live guidance is off for this meeting"
    if meeting.get("Status") == "ENDED" and not allow_ended:
        return "the meeting has ended"
    if meeting.get("TranscriptDeletedAt"):
        return "the meeting's transcript is deleted"
    return None


def run(call_id: str, until: Optional[float] = None) -> Dict[str, Any]:
    """One automatic run. Returns a small result for the log and the tests.

    ``until`` is when the invocation must be done (``time.monotonic``), which
    bounds how many research runs follow it (drain).
    """
    until = until if until is not None else time.monotonic() + DEFAULT_BUDGET_SECONDS
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
        return _analyse(call_id, meeting, settings, state, now, until)
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
    until: float,
) -> Dict[str, Any]:
    lease = state["LeaseUntil"]
    segments = pure.final_segments(
        read_segments(call_id), meeting.get("SpeakerIdentities"), meeting.get("CustomerPhoneNumber")
    )
    # A state row from before ShownSegmentIds was recorded is read by its marker alone.
    shown = _json_field(state, "ShownSegmentIds", []) if "ShownSegmentIds" in state else None
    fresh = live_pass.new_segments(segments, state.get("LastSegmentEndTime"), shown)
    if len(fresh) < settings.min_new_segments:
        release(call_id, lease, ran=False, now=now)
        return {"ran": False, "reason": f"{len(fresh)} new segments"}

    published: Dict[str, Dict[str, Any]] = _json_field(state, "Published", {})
    checklist = pure.checklist_items(meeting.get("Checklist"))
    transcript, names = pure.transcript_block(fresh)
    model_id = model_for(settings)
    research = _ResearchContext.read(call_id, settings, now)
    run_settings = research.run_settings(settings)
    system, messages = pure.build_request(
        meeting,
        run_settings,
        str(state.get("RollingSummary") or ""),
        transcript,
        max(s.end for s in segments),
        pure.render_shown(published, other_guidance(call_id)),
        research.done() if run_settings.check_kinds else None,
    )
    tool = pure.report_tool(run_settings.check_kinds)
    answer = call_model(call_id, model_id, system, messages, tool, RUN_MAX_TOKENS)
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
    # Fact checks and lookups count toward the same cap; their times are kept
    # on the research row.
    chosen = pure.select(
        candidates, published, recent + research.recent, settings.max_items_per_10_min
    )
    published, count = _publish_chosen(call_id, chosen, published, recent, now)
    for key in pure.withdrawals(published, checklist):
        try:
            withdraw(call_id, key, published[key])
        except NotificationError as error:
            LOGGER.info("Guidance item for %s not withdrawn: %s", call_id, error.error_type)
            continue
        published[key]["withdrawn"] = True

    if run_settings.check_kinds:
        research.add(answer.get("checks") if isinstance(answer, Mapping) else None, names, now)
        research.persist(call_id, meeting)

    last_marker = state.get("LastSegmentEndTime")
    release(
        call_id,
        lease,
        ran=True,
        now=now,
        LastSegmentEndTime=live_pass.next_marker(fresh, last_marker),
        ShownSegmentIds=json.dumps(live_pass.shown_after_run(segments, last_marker, shown, fresh)),
        RollingSummary=analysis.summary or str(state.get("RollingSummary") or ""),
        Published=json.dumps(published, ensure_ascii=False),
        PublishTimes=json.dumps(recent),
    )
    # Counts only, never content: a dropped candidate's reason, not its claim.
    LOGGER.info(
        "Live guidance run for %s: segments=%d changes=%d/%d published=%d dropped=%d "
        "checks queued=%d waiting=%d dropped=%s",
        call_id,
        len(fresh),
        changed,
        len(analysis.changes),
        count,
        analysis.dropped,
        len(research.new),
        len(research.pending),
        json.dumps(checks.drop_counts(research.drops)),
    )
    result: Dict[str, Any] = {
        "ran": True,
        "changed": changed,
        "published": count,
        "dropped": analysis.dropped,
    }
    if research.pending and research.can_research():
        try:
            result["research"] = drain(
                call_id, settings, segments, until=until, expires=_expiry(meeting)
            )
        except Exception:  # pylint: disable=broad-except
            # The run itself is done and its lease given back; only the
            # research after it failed, and the queue waits for the next.
            LOGGER.exception("Guidance research after the run of %s failed", call_id)
            result["research"] = {"failed": True}
    return result


# ── fact checks and lookups ──────────────────────────────────────────────────


class _ResearchContext:  # pylint: disable=too-many-instance-attributes
    """What one run knows of fact checks and lookups: the research row, its queue and checks.

    The queue (``Pending``) is a list on the research row, so a run adds to it
    with one atomic append and no lease, while a research run holding the
    research lease takes from its front: what is appended meanwhile stays.
    """

    def __init__(self, settings: pure.Settings, row: Mapping[str, Any], now: float):
        self.settings = settings
        self.checked: Dict[str, Dict[str, Any]] = _json_field(row, "Checked", {})
        self.pending: List[checks.Check] = pending_checks(row)
        self.recent = pure.recent_publishes(_json_field(row, "PublishTimes", []), now)
        unavailable_at = row.get("UnavailableAt")
        self.paused = (
            isinstance(unavailable_at, (int, float))
            and now - float(unavailable_at) < NO_SOURCES_PAUSE_SECONDS
        )
        self.research_runs = int(row.get("RunCount") or 0)
        self.new: List[checks.Check] = []
        self.drops: List[checks.Drop] = []

    @classmethod
    def read(cls, call_id: str, settings: pure.Settings, now: float) -> "_ResearchContext":
        """The context; with no check kind on, nothing is read."""
        if not settings.check_kinds:
            return cls(settings, {}, now)
        context = cls(settings, _research_lease().read(call_id), now)
        checks.mark_dismissed(context.checked, dismissed_ids(call_id))
        return context

    def can_research(self) -> bool:
        return (
            bool(self.settings.check_kinds)
            and not self.paused
            and bool(MEETING_ASSIST_FUNCTION_ARN)
        )

    def run_settings(self, settings: pure.Settings) -> pure.Settings:
        """The settings the run asks with: no candidates while research has nowhere to look.

        Nor once the meeting has used its claims and terms, or its research runs:
        a candidate then could only be dropped.
        """
        runs_left = self.settings.research_runs_per_meeting - self.research_runs
        if not self.can_research() or self.room() <= 0 or runs_left <= 0:
            return dataclasses.replace(settings, fact_checks_enabled=False, lookups_enabled=False)
        return settings

    def done(self) -> str:
        return checks.render_done(self.checked, self.pending)

    def room(self) -> int:
        """Candidates the meeting may still have researched."""
        return self.settings.research_checks_per_meeting - len(self.checked) - len(self.pending)

    def add(self, entries: Any, names: Mapping[str, pure.Segment], now: float) -> None:
        found, drops = checks.parse_checks(
            entries, names, self.settings.check_kinds, self.settings.confidence_threshold, now
        )
        self.new, admitted_drops = checks.admit(
            found, self.checked, self.pending, max(0, self.room()), now
        )
        self.drops = drops + admitted_drops
        self.pending = [*self.pending, *self.new]

    def persist(self, call_id: str, meeting: Mapping[str, Any]) -> None:
        """Append the new candidates to the queue, and the drops to the drop log.

        The queue holds the meeting's words, so nothing is written once its
        transcript is deleted: the meeting is read again just before, since a
        run can finish after the deletion removed the ``gd#`` partition.
        """
        if not self.new and not self.drops:
            return
        current = read_meeting(call_id)
        if current is None or current.get("TranscriptDeletedAt"):
            LOGGER.info("Guidance research for %s: transcript deleted, nothing queued", call_id)
            self.pending = [c for c in self.pending if c not in self.new]
            self.new, self.drops = [], []
            return
        values: Dict[str, Any] = {
            ":new": {"L": [{"S": json.dumps(c.to_record(), ensure_ascii=False)} for c in self.new]},
            ":drops": {
                "L": [{"S": json.dumps(d.to_record(), ensure_ascii=False)} for d in self.drops]
            },
            ":empty": {"L": []},
            ":call": {"S": call_id},
        }
        sets = [
            "Pending = list_append(if_not_exists(Pending, :empty), :new)",
            "DropLog = list_append(if_not_exists(DropLog, :empty), :drops)",
            "CallId = :call",
        ]
        expires = _expiry(meeting)
        if expires:
            values[":ttl"] = {"N": str(int(expires))}
            sets.append("ExpiresAfter = if_not_exists(ExpiresAfter, :ttl)")
        dynamodb.update_item(
            TableName=EVENT_SOURCING_TABLE,
            Key=_research_lease().key(call_id),
            UpdateExpression="SET " + ", ".join(sets),
            ExpressionAttributeValues=values,
        )


def pending_checks(row: Mapping[str, Any]) -> List[checks.Check]:
    """The research row's queue, oldest first."""
    queue: List[checks.Check] = []
    for value in row.get("Pending") or []:
        try:
            check = checks.Check.from_record(json.loads(value) if isinstance(value, str) else value)
        except ValueError:
            check = None
        if check is not None:
            queue.append(check)
    return queue


def drain(  # pylint: disable=too-many-arguments
    call_id: str,
    settings: pure.Settings,
    segments: Sequence[pure.Segment],
    *,
    until: float,
    at_end: bool = False,
    expires: Optional[int] = None,
) -> Dict[str, Any]:
    """Research the queue in batches while this invocation has time, and the meeting runs left.

    Each batch is one research run under the research lease: the first waits
    RESEARCH_INTERVAL_SECONDS after the last, the ones that follow in the same
    invocation do not. A batch starts only when its research can finish before
    ``until``; what is left waits for the next run, or for the run at the
    meeting's end (``at_end``), which may still publish. When the meeting has
    used its research runs, what waits is dropped and logged. Each claim gives
    the research row the meeting's expiry (``expires``) if it has none, so a row
    a claim creates again after the transcript's deletion still expires.
    """
    totals = {"runs": 0, "researched": 0, "findings": 0, "published": 0}
    continuing = False
    while time.monotonic() + settings.research_seconds_per_run + BATCH_SLACK_SECONDS < until:
        now = time.time()
        row = _research_lease().claim(
            call_id,
            now=now,
            interval_seconds=RESEARCH_INTERVAL_SECONDS,
            max_runs=settings.research_runs_per_meeting,
            expires=expires,
            # The run at the meeting's end does not wait the interval: a batch
            # that started just before END must not keep what waits waiting.
            ignore_interval=continuing or at_end,
        )
        if row is None:
            _drop_when_out_of_runs(call_id, settings)
            break
        queue = pending_checks(row)
        if not queue:
            _research_lease().release(call_id, row["LeaseUntil"], ran=False, now=now)
            break
        batch = queue[: checks.MAX_PER_RESEARCH]
        outcome = research_run(call_id, settings, row, batch, segments, at_end=at_end)
        totals["runs"] += 1
        for key in ("researched", "findings", "published"):
            totals[key] += int(outcome.get(key) or 0)
        continuing = True
    left = len(pending_checks(_research_lease().read(call_id)))
    LOGGER.info(
        "Guidance research drain for %s: runs=%d researched=%d published=%d deferred=%d end=%s",
        call_id,
        totals["runs"],
        totals["researched"],
        totals["published"],
        left,
        at_end,
    )
    return {**totals, "deferred": left}


def _drop_when_out_of_runs(call_id: str, settings: pure.Settings) -> None:
    """Drop, and log, what waits once the meeting has used its research runs."""
    row = _research_lease().read(call_id)
    queue = pending_checks(row)
    lease = row.get("LeaseUntil")
    if not queue or int(row.get("RunCount") or 0) < settings.research_runs_per_meeting:
        return
    if isinstance(lease, (int, float)) and lease >= time.time():
        return
    now = time.time()
    drops = [checks.Drop(c.kind, c.subject, checks.REASON_RUNS_CAP, now) for c in queue]
    try:
        dynamodb.update_item(
            TableName=EVENT_SOURCING_TABLE,
            Key=_research_lease().key(call_id),
            UpdateExpression=(
                "SET Pending = :empty, DropLog = list_append(if_not_exists(DropLog, :empty), :d)"
            ),
            # attribute_exists(PK): never recreate a row the transcript's
            # deletion removed.
            ConditionExpression=(
                "attribute_exists(PK) AND (attribute_not_exists(LeaseUntil) OR LeaseUntil < :now)"
            ),
            ExpressionAttributeValues={
                ":empty": {"L": []},
                ":d": {"L": [{"S": json.dumps(d.to_record())} for d in drops]},
                ":now": {"N": str(now)},
            },
        )
    except ClientError as error:
        if not _conflict(error):
            raise
        return
    LOGGER.info(
        "Guidance research for %s: %d waiting dropped, research runs per meeting reached",
        call_id,
        len(drops),
    )


def research_request(
    call_id: str,
    meeting: Mapping[str, Any],
    settings: pure.Settings,
    batch: Sequence[checks.Check],
    segments: Sequence[pure.Segment],
) -> Dict[str, Any]:
    """The meeting assistant function's guidance_research request for one batch."""
    return {
        "action": "guidance_research",
        "callId": call_id,
        # Past meetings are searched as the meeting's owner sees them.
        "ownerEmail": str(meeting.get("Owner") or ""),
        "modelId": model_for(settings),
        "candidates": checks.research_candidates(batch, segments),
        "sources": settings.research_sources(),
        "mcpTools": list(settings.research_mcp_tools),
        "maxToolCalls": settings.research_tool_calls_per_run,
        "deadlineSeconds": settings.research_seconds_per_run,
    }


def invoke_research(request: Mapping[str, Any], seconds: int) -> Dict[str, Any]:
    """The meeting assistant function's answer to a research request."""
    response = _lambda_client(seconds + 45).invoke(
        FunctionName=MEETING_ASSIST_FUNCTION_ARN,
        InvocationType="RequestResponse",
        Payload=json.dumps(request, ensure_ascii=False).encode("utf-8"),
    )
    if response.get("FunctionError"):
        raise ResearchError(str(response.get("FunctionError")))
    answer = json.loads(response["Payload"].read() or b"{}")
    if not isinstance(answer, dict):
        raise ResearchError("the answer is not an object")
    return answer


def _sources(value: Any) -> Dict[str, Dict[str, str]]:
    """The research answer's sources, ``R<n>`` to ``{title, url, via}``, each value text."""
    sources: Dict[str, Dict[str, str]] = {}
    for source_id, source in value.items() if isinstance(value, Mapping) else []:
        if not isinstance(source_id, str) or not isinstance(source, Mapping):
            continue
        url = str(source.get("url") or "")
        if not url.startswith(("https://", "http://")) or any(c in url for c in " <>\n"):
            url = ""
        sources[source_id] = {
            "title": str(source.get("title") or "")[:200],
            "url": url[:2000],
            "via": str(source.get("via") or "")[:80],
        }
    return sources


def publish_check(call_id: str, item: checks.Publication) -> Dict[str, Any]:
    return publish_meeting_notification(
        call_id,
        category=GUIDANCE,
        type=item.type,
        title=item.title,
        body=item.body,
        severity=item.severity,
        refs=item.refs,
        dedup_key=item.dedup_key,
        actions=item.actions(),
        source=pure.SOURCE,
    )


def research_run(  # pylint: disable=too-many-locals,too-many-arguments
    call_id: str,
    settings: pure.Settings,
    row: Mapping[str, Any],
    batch: Sequence[checks.Check],
    segments: Sequence[pure.Segment],
    *,
    at_end: bool = False,
) -> Dict[str, Any]:
    """Research one batch under the research lease, and publish what holds up.

    Every candidate in the batch is recorded as checked whatever happens, so a
    candidate that failed is not researched again and again. Nothing is
    published once the meeting has ended, its transcript is deleted or live
    guidance was turned off for it meanwhile, nor for a candidate whose earlier
    item was dismissed.
    """
    now = time.time()
    lease = row["LeaseUntil"]
    checked: Dict[str, Dict[str, Any]] = _json_field(row, "Checked", {})
    # Recorded as failed first, so a run that stops on an error below still
    # releases its lease and does not research the batch again.
    for check in batch:
        checked = checks.remember(checked, check, now, "failed")
    recent = pure.recent_publishes(_json_field(row, "PublishTimes", []), now)
    fields: Dict[str, Any] = {}
    try:
        return _research_batch(
            call_id, settings, batch, segments, checked, recent, fields, now, at_end
        )
    except Exception:  # pylint: disable=broad-except
        LOGGER.exception("Guidance research for %s stopped; its batch is not retried", call_id)
        return {"researched": len(batch), "findings": 0, "published": 0}
    finally:
        release_research(
            call_id,
            lease,
            now,
            taken=len(batch),
            drop_log=len(row.get("DropLog") or []),
            Checked=json.dumps(checked, ensure_ascii=False),
            PublishTimes=json.dumps(recent),
            **fields,
        )


def _research_batch(  # pylint: disable=too-many-arguments,too-many-locals
    call_id: str,
    settings: pure.Settings,
    batch: Sequence[checks.Check],
    segments: Sequence[pure.Segment],
    checked: Dict[str, Dict[str, Any]],
    recent: List[float],
    fields: Dict[str, Any],
    now: float,
    at_end: bool,
) -> Dict[str, Any]:
    """research_run's work, which updates ``checked``, ``recent`` and ``fields`` in place."""
    meeting = read_meeting(call_id) or {}
    try:
        answer = invoke_research(
            research_request(call_id, meeting, settings, batch, segments),
            settings.research_seconds_per_run,
        )
    except Exception as error:  # noqa: BLE001 -- recorded below, and not retried
        LOGGER.warning("Guidance research for %s failed: %s", call_id, type(error).__name__)
        answer = {"ok": False, "reason": "failed"}
    reason = str(answer.get("reason") or "")
    if reason != "failed" and not isinstance(answer.get("findings"), list):
        # Not a research answer at all: the Agent Assist function is another
        # one. Treated as a deployment with nowhere to look.
        reason = "not a research function"
    if reason in ("no sources", "not a research function"):
        fields["UnavailableAt"] = now
    findings = {str(f.get("id")): f for f in answer.get("findings") or [] if isinstance(f, Mapping)}
    sources = _sources(answer.get("sources"))
    # A batch may publish after the meeting ended: one under way at END is the
    # end's research too, and its findings are what the meeting left open.
    stop = why_not(read_meeting(call_id), settings, allow_ended=True)
    checks.mark_dismissed(checked, dismissed_ids(call_id))
    others = pure.recent_publishes(_json_field(read_state(call_id), "PublishTimes", []), now)
    room = settings.max_items_per_10_min - len(others) - len(recent)
    count = 0
    unsearched: List[checks.Drop] = []
    for number, check in enumerate(batch, start=1):
        finding = findings.get(f"C{number}")
        verdict = str(finding.get("verdict")) if finding else (reason or "unanswered")
        item = None
        if finding and not stop and not (checked.get(check.key) or {}).get("dismissed"):
            item = checks.publication(
                check,
                finding,
                sources,
                settings.research_confidence_threshold,
                settings.fact_checks_show_supported,
            )
        notification_id = None
        if item is not None and room > 0:
            try:
                notification_id = publish_check(call_id, item).get("NotificationId")
                count += 1
                room -= 1
                recent.append(now)
            except (NotificationRateLimited, NotificationTranscriptDeleted) as error:
                LOGGER.info(
                    "Guidance research publish for %s stopped: %s", call_id, error.error_type
                )
                room = 0
            except NotificationError as error:
                LOGGER.warning(
                    "Guidance research item for %s refused: %s", call_id, error.error_type
                )
        if reason in UNSEARCHED:
            # Nothing was searched: not counted as checked, logged as dropped.
            checked.pop(check.key, None)
            unsearched.append(checks.Drop(check.kind, check.subject, reason, now))
            continue
        # The shared record, trimmed in place (research_run stores it).
        kept = checks.remember(checked, check, now, verdict, notification_id)
        if kept is not checked:
            checked.clear()
            checked.update(kept)
    # Counts only, never content.
    LOGGER.info(
        "Guidance research for %s: candidates=%d findings=%d published=%d toolCalls=%s reason=%s",
        call_id,
        len(batch),
        len(findings),
        count,
        answer.get("toolCalls"),
        reason or stop or "none",
    )
    if unsearched:
        fields["_drops"] = unsearched
    return {"researched": len(batch), "findings": len(findings), "published": count}


def release_research(  # pylint: disable=too-many-arguments
    call_id: str, lease: Any, now: float, *, taken: int, drop_log: int = 0, **fields: Any
) -> None:
    """Give the research lease back, count the run, and take its batch off the queue.

    The batch is the front of the queue (``taken`` entries): only the lease
    holder removes entries, and runs only append, so the first ``taken`` are
    still this batch's. The drop log is cut to its newest MAX_DROP_LOG the same
    way. Conditional on this run's lease, as LivePass.release is.
    """
    drops = fields.pop("_drops", None) or []
    names: Dict[str, str] = {}
    values: Dict[str, Any] = {
        ":lease": {"N": str(int(lease))},
        ":now": {"N": str(now)},
        ":zero": {"N": "0"},
        ":one": {"N": "1"},
    }
    sets = ["LastRunAt = :now", "RunCount = if_not_exists(RunCount, :zero) + :one"]
    for number, (name, value) in enumerate(fields.items()):
        names[f"#f{number}"] = name
        values[f":f{number}"] = (
            {"N": str(value)}
            if isinstance(value, (int, float)) and not isinstance(value, bool)
            else {"S": str(value)}
        )
        sets.append(f"#f{number} = :f{number}")
    removes = ["LeaseUntil"] + [f"Pending[{index}]" for index in range(taken)]
    excess = max(0, drop_log - checks.MAX_DROP_LOG)
    removes += [f"DropLog[{index}]" for index in range(excess)]
    kwargs: Dict[str, Any] = {
        "TableName": EVENT_SOURCING_TABLE,
        "Key": _research_lease().key(call_id),
        "UpdateExpression": "SET " + ", ".join(sets) + " REMOVE " + ", ".join(removes),
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
        LOGGER.warning("The guidance research lease for %s was no longer this run's", call_id)
        return
    if drops:
        # Its own update: the release removes from DropLog, and one update may
        # not both remove from a list and append to it.
        dynamodb.update_item(
            TableName=EVENT_SOURCING_TABLE,
            Key=_research_lease().key(call_id),
            UpdateExpression="SET DropLog = list_append(if_not_exists(DropLog, :e), :d)",
            ConditionExpression="attribute_exists(PK)",
            ExpressionAttributeValues={
                ":e": {"L": []},
                ":d": {"L": [{"S": json.dumps(d.to_record())} for d in drops]},
            },
        )


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


def end_run(call_id: str, until: float) -> Dict[str, Any]:
    """The research run at the meeting's end: what still waits is researched and may be published.

    Queued by the call event processor when a meeting ends, as the whiteboards'
    last run is. Nothing happens for a meeting with live guidance off, fact
    checks and lookups off, or nothing waiting.
    """
    settings = load_settings()
    meeting = read_meeting(call_id)
    reason = why_not(meeting, settings, allow_ended=True)
    if reason or meeting is None:
        return {"ran": False, "reason": reason}
    if not settings.check_kinds or not MEETING_ASSIST_FUNCTION_ARN:
        return {"ran": False, "reason": "fact checks and lookups are off"}
    segments = pure.final_segments(
        read_segments(call_id), meeting.get("SpeakerIdentities"), meeting.get("CustomerPhoneNumber")
    )
    found = final_candidates(call_id, meeting, settings, segments)
    if not pending_checks(_research_lease().read(call_id)):
        return {"ran": False, "reason": "nothing waits for research", "found": found}
    research = drain(
        call_id, settings, segments, until=until, at_end=True, expires=_expiry(meeting)
    )
    return {"ran": True, "found": found, "research": research}


def final_candidates(
    call_id: str,
    meeting: Mapping[str, Any],
    settings: pure.Settings,
    segments: Sequence[pure.Segment],
) -> int:
    """At the meeting's end, the candidates in what no run read: how many were queued.

    The runs follow speech at most every interval, so the meeting's last
    minutes may never have been read. This reads them once, under the run
    lease (whatever the interval), and asks for candidates only: the meeting is
    over, so no other guidance and no checklist change is published.
    """
    now = time.time()
    state = _lease().claim(
        call_id,
        now=now,
        interval_seconds=0,
        max_runs=settings.max_runs_per_meeting,
        expires=_expiry(meeting),
        ignore_interval=True,
    )
    if state is None:
        return 0
    lease = state["LeaseUntil"]
    try:
        shown = _json_field(state, "ShownSegmentIds", []) if "ShownSegmentIds" in state else None
        fresh = live_pass.new_segments(segments, state.get("LastSegmentEndTime"), shown)
        research = _ResearchContext.read(call_id, settings, now)
        run_settings = research.run_settings(settings)
        if not fresh or not run_settings.check_kinds:
            release(call_id, lease, ran=False, now=now)
            return 0
        transcript, names = pure.transcript_block(fresh)
        system, messages = pure.build_request(
            meeting,
            run_settings,
            str(state.get("RollingSummary") or ""),
            transcript,
            max(s.end for s in segments),
            "(the meeting has ended: report checks only)",
            research.done(),
        )
        answer = call_model(
            call_id,
            model_for(settings),
            system,
            messages,
            pure.report_tool(run_settings.check_kinds),
            RUN_MAX_TOKENS,
        )
        research.add(answer.get("checks") if isinstance(answer, Mapping) else None, names, now)
        research.persist(call_id, meeting)
        last_marker = state.get("LastSegmentEndTime")
        release(
            call_id,
            lease,
            ran=True,
            now=now,
            LastSegmentEndTime=live_pass.next_marker(fresh, last_marker),
            ShownSegmentIds=json.dumps(
                live_pass.shown_after_run(segments, last_marker, shown, fresh)
            ),
        )
    except Exception:  # pylint: disable=broad-except
        LOGGER.exception("The end-of-meeting read of %s failed", call_id)
        release(call_id, lease, ran=True, now=now)
        return 0
    LOGGER.info(
        "Live guidance at the end of %s: segments=%d checks queued=%d dropped=%s",
        call_id,
        len(fresh),
        len(research.new),
        json.dumps(checks.drop_counts(research.drops)),
    )
    return len(research.new)


def handler(event, context):
    """Entry point for the AppSync resolver (a digest) and the backend invoke (a run)."""
    arguments = event.get("arguments") or {}
    call_id = arguments.get("CallId") or event.get("CallId")
    if not call_id or not isinstance(call_id, str):
        raise ValueError("no CallId in event")
    identity = event.get("identity")
    # Only the backend asks for a run: a browser caller arrives with an identity,
    # and its request is a digest.
    until = time.monotonic() + (
        context.get_remaining_time_in_millis() / 1000.0 - 10
        if context is not None and hasattr(context, "get_remaining_time_in_millis")
        else DEFAULT_BUDGET_SECONDS
    )
    if identity is None and event.get("Trigger") == END_TRIGGER:
        result = end_run(call_id, until)
        LOGGER.info("Live guidance at the end of %s: %s", call_id, result.get("reason") or "ran")
        return result
    if identity is None and event.get("Trigger") == "auto":
        result = run(call_id, until)
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
