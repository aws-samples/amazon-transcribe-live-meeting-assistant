#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""A meeting's whiteboards: the AppSync resolver, and the assistant that updates them.

Reached two ways:

* **The resolver** for ``listMeetingWhiteboards``, ``getMeetingWhiteboard``,
  ``listMeetingWhiteboardVersions``, ``createMeetingWhiteboard``,
  ``updateMeetingWhiteboard``, ``deleteMeetingWhiteboard``,
  ``updateCallWhiteboards`` and ``requestWhiteboardUpdate``, through
  meetingWhiteboards.request.vtl. Who may read is the getCall rule and who may
  write the updateCallAgenda rule: an Admin, the meeting's owner, a user it is
  shared with, or a backend role. A write is one DynamoDB transaction that
  checks the meeting row (it exists, its transcript is not deleted, and the
  caller may change it, restated as a condition), checks the whiteboard's
  version as read, and stores the new version beside the whiteboard, so access
  and concurrency are judged at the moment of the write.
* **A run** of the assistant -- invoked asynchronously by the call event
  processor for a finished speech segment of a meeting whose
  ``WhiteboardsEnabled`` is true, once more when the meeting ends, and on
  request. A run takes the meeting's lease (lma_live_pass, on ``wb#<CallId>`` /
  ``updater``), reads what was said since the last run, makes one model call for
  all the whiteboards it updates, and writes each change through the
  ``updateMeetingWhiteboard`` mutation as the assistant, so open pages update
  and the resolver's rules apply to it as to anyone: a panel a user wrote is
  never changed by it.

Everything is stored under ``wb#<CallId>``: each whiteboard (``b#<id>``), each
of its versions (``v#<id>#<version>``), a count of the meeting's whiteboards
(``index``), a per-minute write counter (``r#<minute>``) and the run's state
(``updater``). Every row takes the transcript's expiry, because a whiteboard is
drawn from the transcript, and the partition is deleted when the transcript is
deleted after the summary and when the meeting is deleted. See
docs/meeting-whiteboards.md.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Mapping, Optional, Tuple

import boto3
import updater
from bedrock_compat import (
    PROMPT_CACHE_MESSAGES,
    PROMPT_CACHE_SYSTEM,
    PROMPT_CACHE_TOOLS,
    additional_request_fields,
    build_inference_config,
    prompt_cache_sections,
)
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.config import Config
from botocore.exceptions import ClientError

# pylint: disable=import-error
from lma_config import LANGUAGE_MODELS, MEETING_ASSIST, read_setting, resolve_model_id
from lma_live_pass import LivePass
from lma_metering import MeteringWriter, bedrock_service, bedrock_usage
from lma_notifications import NotificationError, appsync_graphql, transcript_expires_after
from lma_whiteboards import rules

# pylint: enable=import-error

# Standard logging rather than Powertools: the layer that carries Powertools is
# built for arm64 only, as for the live guidance analyzer.
LOGGER = logging.getLogger()
LOGGER.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

EVENT_SOURCING_TABLE = os.environ.get("EVENT_SOURCING_TABLE", "")
ADMIN_GROUP = os.environ.get("ADMIN_GROUP", "Admin")
BEDROCK_REGION = os.environ.get("BEDROCK_REGION_OVERRIDE") or os.environ.get("AWS_REGION", "")
#: The model when the configuration cannot be read; the Meeting Assistant
#: model's shipped default in setting_defaults.json.
FALLBACK_MODEL_ID = "global.anthropic.claude-haiku-5-5"
#: The metering context the meeting cost estimate lists the assistant's runs under.
METERING_CONTEXT = "MeetingWhiteboards"
#: Longer than the function's timeout, so a stopped run cannot hold its meeting longer.
LEASE_SECONDS = int(os.environ.get("LEASE_SECONDS", "180"))
RUN_MAX_TOKENS = 2500
#: Write attempts on one meeting's whiteboards in a UTC minute, people and assistant
#: together, counted before the write whether or not it is stored.
WRITES_PER_MINUTE = 60
MAX_VERSION_PAGE = 50
MAX_TRANSCRIPT_PAGES = 100

CLIENT_CONFIG = Config(retries={"mode": "adaptive", "max_attempts": 3})
DYNAMODB = boto3.client("dynamodb", config=CLIENT_CONFIG)
SERIALIZER = TypeSerializer()
DESERIALIZER = TypeDeserializer()
LIVE = LivePass(
    DYNAMODB,
    EVENT_SOURCING_TABLE,
    prefix="wb#",
    sort_key="updater",
    lease_seconds=LEASE_SECONDS,
    name="whiteboards",
)
# Every model call is billed to its meeting. Inert when METERING_TABLE_NAME is unset.
METERING = MeteringWriter(source="meeting-whiteboards", client=DYNAMODB)
_BEDROCK = None


def bedrock():
    """The Bedrock client, made on first use (resolver invocations never need it)."""
    global _BEDROCK  # pylint: disable=global-statement
    if _BEDROCK is None:
        _BEDROCK = boto3.client(
            "bedrock-runtime",
            region_name=BEDROCK_REGION or None,
            config=Config(retries={"max_attempts": 50, "mode": "adaptive"}),
        )
    return _BEDROCK


# ── errors ───────────────────────────────────────────────────────────────────
# AppSync reports a Lambda error with the exception's class name as its type,
# which the page and the MCP tools read, so each type is a class of its own.


class ApiError(Exception):
    """A refusal, reported to the caller under ``error_type``."""

    error_type = "WhiteboardInvalid"


def _error_class(name: str) -> type:
    return type(name, (ApiError,), {"error_type": name})


ERRORS: Dict[str, type] = {
    name: _error_class(name)
    for name in (
        "Unauthorized",
        "BadRequest",
        "WhiteboardInvalid",
        "WhiteboardTooLarge",
        "WhiteboardLimit",
        "WhiteboardNotFound",
        "WhiteboardConflict",
        "WhiteboardTranscriptDeleted",
        "WhiteboardRateLimited",
        "WhiteboardUserPanel",
        "WhiteboardAssistantOff",
    )
}


def refuse(error_type: str, message: str) -> ApiError:
    return ERRORS.get(error_type, ERRORS["WhiteboardInvalid"])(message)


def _rules_error(error: rules.WhiteboardError) -> ApiError:
    return refuse(error.error_type, str(error))


# ── values ───────────────────────────────────────────────────────────────────


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _plain(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, (set, frozenset)):
        return sorted(_plain(v) for v in value)
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    return value


def _dynamo(value: Any) -> Any:
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, list):
        return [_dynamo(v) for v in value]
    if isinstance(value, dict):
        return {k: _dynamo(v) for k, v in value.items()}
    return value


def _item(value: Mapping[str, Any]) -> Dict[str, Any]:
    return {k: SERIALIZER.serialize(_dynamo(v)) for k, v in value.items() if v is not None}


def _rows(items: Any) -> List[Dict[str, Any]]:
    """Deserialised items of a query page."""
    return [row for row in (_from_item(i) for i in items or []) if row is not None]


def _from_item(item: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    if not item:
        return None
    return {k: _plain(DESERIALIZER.deserialize(v)) for k, v in item.items()}


def _conflict(error: ClientError) -> bool:
    return error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


# ── the caller and the meeting ──────────────────────────────────────────────


def caller_of(event: Mapping[str, Any]) -> Dict[str, Any]:
    """Who is asking, from the AppSync ``identity`` block only.

    A request signed with IAM by one of LMA's own roles carries an assumed-role
    ``userArn`` and no user-pool claims, and is the backend.
    """
    identity = event.get("identity") or {}
    claims = identity.get("claims") or {}
    groups = claims.get("cognito:groups") or identity.get("groups") or []
    if isinstance(groups, str):
        groups = [g.strip() for g in groups.split(",") if g.strip()]
    user_arn = str(identity.get("userArn") or "")
    is_backend = not claims and ":assumed-role/" in user_arn
    return {
        "username": "" if is_backend else str(identity.get("username") or ""),
        "is_admin": not is_backend and ADMIN_GROUP in groups,
        "is_backend": is_backend,
    }


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


def _meeting_key(call_id: str) -> Dict[str, Any]:
    return {"PK": {"S": f"c#{call_id}"}, "SK": {"S": f"c#{call_id}"}}


def _key(call_id: str, sort_key: str) -> Dict[str, Any]:
    return {"PK": {"S": rules.partition(call_id)}, "SK": {"S": sort_key}}


def read_meeting(call_id: str, *, full: bool = False) -> Optional[Dict[str, Any]]:
    """The meeting row, read consistently: what the rules need, or all of it."""
    kwargs: Dict[str, Any] = {
        "TableName": EVENT_SOURCING_TABLE,
        "Key": _meeting_key(call_id),
        "ConsistentRead": True,
    }
    if not full:
        kwargs["ProjectionExpression"] = (
            "CallId, #o, SharedWith, TranscriptDeletedAt, ExpiresAfter, WhiteboardsEnabled, #s"
        )
        kwargs["ExpressionAttributeNames"] = {"#o": "Owner", "#s": "Status"}
    return _from_item(DYNAMODB.get_item(**kwargs).get("Item"))


def may_see(caller: Mapping[str, Any], meeting: Mapping[str, Any]) -> bool:
    """The getCall rule."""
    if caller["is_backend"] or caller["is_admin"]:
        return True
    username = caller["username"]
    return bool(username) and (
        meeting.get("Owner") == username
        or username in shared_with_entries(meeting.get("SharedWith"))
    )


def access_condition(
    caller: Mapping[str, Any], meeting: Mapping[str, Any]
) -> Tuple[str, Dict, Dict]:
    """The caller's right to change the meeting, as a condition restating the read.

    The rule and its form are the meeting preparation resolver's.
    """
    if caller["is_backend"] or caller["is_admin"]:
        return "", {}, {}
    username = caller["username"]
    if username and meeting.get("Owner") == username:
        return (
            "#accessOwner = :accessCaller",
            {"#accessOwner": "Owner"},
            {":accessCaller": {"S": username}},
        )
    shared = meeting.get("SharedWith")
    if username and username in shared_with_entries(shared):
        names = {"#accessOwner": "Owner", "#accessShared": "SharedWith"}
        if isinstance(shared, str):
            return (
                "(#accessOwner = :accessCaller OR #accessShared = :accessSharedAsRead)",
                names,
                {":accessCaller": {"S": username}, ":accessSharedAsRead": {"S": shared}},
            )
        return (
            "(#accessOwner = :accessCaller OR ((attribute_type(#accessShared, :accessList) "
            "OR attribute_type(#accessShared, :accessSet)) AND contains(#accessShared, :accessCaller)))",
            names,
            {
                ":accessCaller": {"S": username},
                ":accessList": {"S": "L"},
                ":accessSet": {"S": "SS"},
            },
        )
    raise refuse("Unauthorized", "Unauthorized: you may not change this meeting")


def required_call_id(data: Mapping[str, Any]) -> str:
    call_id = data.get("CallId")
    if not isinstance(call_id, str) or not call_id:
        raise refuse("BadRequest", "CallId is required")
    return call_id


def load_for_read(caller: Mapping[str, Any], call_id: Any) -> Dict[str, Any]:
    if not isinstance(call_id, str) or not call_id:
        raise refuse("BadRequest", "CallId is required")
    meeting = read_meeting(call_id)
    if meeting is None or not may_see(caller, meeting):
        # The same answer as for a meeting that does not exist, as in getCall.
        raise refuse("Unauthorized", "Unauthorized: you may not see this meeting")
    return meeting


def load_for_write(
    caller: Mapping[str, Any], call_id: Any
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """The meeting, and the ConditionCheck that restates what a write depends on."""
    if not isinstance(call_id, str) or not call_id:
        raise refuse("BadRequest", "CallId is required")
    meeting = read_meeting(call_id)
    if meeting is None:
        if caller["is_backend"]:
            raise refuse("WhiteboardNotFound", f"There is no meeting {call_id}")
        raise refuse("Unauthorized", "Unauthorized: you may not change this meeting")
    condition, names, values = access_condition(caller, meeting)
    if meeting.get("TranscriptDeletedAt"):
        raise refuse(
            "WhiteboardTranscriptDeleted",
            "The meeting's transcript is deleted, and its whiteboards with it",
        )
    clauses = ["attribute_exists(PK)", "attribute_not_exists(#accessDeleted)"]
    names = {**names, "#accessDeleted": "TranscriptDeletedAt"}
    if condition:
        clauses.append(condition)
    check = {
        "ConditionCheck": {
            "TableName": EVENT_SOURCING_TABLE,
            "Key": _meeting_key(call_id),
            "ConditionExpression": " AND ".join(clauses),
            "ExpressionAttributeNames": names,
            **({"ExpressionAttributeValues": values} if values else {}),
        }
    }
    return meeting, check


def actor_and_source(caller: Mapping[str, Any], data: Mapping[str, Any]) -> Tuple[str, str]:
    """Who a change is made by, and whether it is a user's or the assistant's.

    A signed-in user's change is always their own. A backend role names the
    source: ``user`` when it acts for a person (whom ``UpdatedBy`` names), or
    ``assistant``.
    """
    if not caller["is_backend"]:
        return caller["username"], rules.SOURCE_USER
    source = rules.check_source(data.get("Source"))
    if source is None:
        raise refuse("BadRequest", "A backend change names its Source: user or assistant")
    default = rules.ASSISTANT_LABEL if source == rules.SOURCE_ASSISTANT else "LMA"
    return rules.attribution(data.get("UpdatedBy"), default), source


def expiry(meeting: Mapping[str, Any]) -> Optional[int]:
    """The transcript's expiry, capped at the meeting's: whiteboards follow the transcript."""
    expires = transcript_expires_after()
    meeting_expiry = meeting.get("ExpiresAfter")
    if isinstance(meeting_expiry, (int, float)) and meeting_expiry > 0:
        expires = min(expires, int(meeting_expiry)) if expires else int(meeting_expiry)
    return expires


def load_settings() -> rules.Settings:
    try:
        raw = read_setting(MEETING_ASSIST.name, "whiteboards", default={})
    except Exception:  # pylint: disable=broad-except
        LOGGER.warning("Could not read the Whiteboards settings; using the shipped ones")
        raw = {}
    return rules.Settings.from_raw(raw)


# ── the store ────────────────────────────────────────────────────────────────


def query_partition(call_id: str, prefix: str, **extra: Any) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    start = None
    for _page in range(MAX_TRANSCRIPT_PAGES):
        kwargs: Dict[str, Any] = {
            "TableName": EVENT_SOURCING_TABLE,
            "KeyConditionExpression": "PK = :pk AND begins_with(SK, :prefix)",
            "ExpressionAttributeValues": {
                ":pk": {"S": rules.partition(call_id)},
                ":prefix": {"S": prefix},
            },
            **extra,
        }
        if start:
            kwargs["ExclusiveStartKey"] = start
        response = DYNAMODB.query(**kwargs)
        items.extend(_rows(response.get("Items")))
        start = response.get("LastEvaluatedKey")
        if not start:
            break
    return items


def read_boards(call_id: str) -> List[Dict[str, Any]]:
    return rules.sort_whiteboards(query_partition(call_id, "b#"))


def read_board(call_id: str, whiteboard_id: str) -> Optional[Dict[str, Any]]:
    return _from_item(
        DYNAMODB.get_item(
            TableName=EVENT_SOURCING_TABLE,
            Key=_key(call_id, rules.head_key(whiteboard_id)),
            ConsistentRead=True,
        ).get("Item")
    )


def count_write(call_id: str, expires: Optional[int]) -> None:
    """One more write this minute; refused past WRITES_PER_MINUTE."""
    minute = datetime.now(timezone.utc).strftime("%Y%m%d%H%M")
    try:
        DYNAMODB.update_item(
            TableName=EVENT_SOURCING_TABLE,
            Key=_key(call_id, f"r#{minute}"),
            UpdateExpression="ADD #c :one SET ExpiresAfter = :ttl",
            ConditionExpression="attribute_not_exists(#c) OR #c < :max",
            ExpressionAttributeNames={"#c": "Count"},
            ExpressionAttributeValues={
                ":one": {"N": "1"},
                ":max": {"N": str(WRITES_PER_MINUTE)},
                ":ttl": {"N": str(min(int(time.time()) + 3600, expires or 2**31))},
            },
        )
    except ClientError as error:
        if _conflict(error):
            raise refuse(
                "WhiteboardRateLimited",
                f"A meeting's whiteboards take at most {WRITES_PER_MINUTE} changes a minute",
            ) from error
        raise


def _head_row(call_id: str, board: Mapping[str, Any], expires: Optional[int]) -> Dict[str, Any]:
    return _item(
        {
            **board,
            "PK": rules.partition(call_id),
            "SK": rules.head_key(board["WhiteboardId"]),
            "CallId": call_id,
            "ExpiresAfter": expires,
        }
    )


def _version_row(  # pylint: disable=too-many-arguments
    call_id: str,
    board: Mapping[str, Any],
    source: str,
    changed: List[str],
    expires: Optional[int],
) -> Dict[str, Any]:
    return _item(
        {
            "PK": rules.partition(call_id),
            "SK": rules.version_key(board["WhiteboardId"], board["Version"]),
            "CallId": call_id,
            "WhiteboardId": board["WhiteboardId"],
            "Version": board["Version"],
            "Title": board.get("Title"),
            "Template": board.get("Template"),
            "Instructions": board.get("Instructions"),
            "AssistantUpdates": board.get("AssistantUpdates"),
            "Panels": board.get("Panels"),
            "Source": source,
            "ChangedPanels": changed,
            "UpdatedBy": board.get("UpdatedBy"),
            "UpdatedAt": board.get("UpdatedAt"),
            "ExpiresAfter": expires,
        }
    )


def _transact(items: List[Dict[str, Any]]) -> Optional[List[str]]:
    """Write ``items`` together; None, or the cancellation reason code of each item."""
    try:
        DYNAMODB.transact_write_items(TransactItems=items)  # type: ignore[arg-type]
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") != "TransactionCanceledException":
            raise
        reasons = error.response.get("CancellationReasons") or []
        return [str((r or {}).get("Code") or "None") for r in reasons] or ["Unknown"]
    return None


def _meeting_refusal(caller: Mapping[str, Any], call_id: str) -> ApiError:
    """Why the meeting's condition failed, from the meeting as it is now."""
    meeting = read_meeting(call_id)
    if meeting is None:
        return refuse("WhiteboardNotFound", f"There is no meeting {call_id}")
    if meeting.get("TranscriptDeletedAt"):
        return refuse(
            "WhiteboardTranscriptDeleted",
            "The meeting's transcript is deleted, and its whiteboards with it",
        )
    return refuse("Unauthorized", "Unauthorized: you may not change this meeting")


def _answer(
    call_id: str, board: Mapping[str, Any], meeting: Mapping[str, Any], **extra: Any
) -> Dict:
    """A mutation's answer: the whiteboard, with the meeting's Owner and SharedWith,
    which dDbPutCondition.response.vtl publishes in the subscription's form."""
    out = {k: v for k, v in board.items() if k not in ("PK", "SK", "ExpiresAfter")}
    out.update(CallId=call_id, Owner=meeting.get("Owner"), SharedWith=meeting.get("SharedWith"))
    out.update(extra)
    return out


# ── the resolver's operations ───────────────────────────────────────────────


def list_whiteboards(caller: Mapping[str, Any], arguments: Mapping[str, Any]) -> Dict[str, Any]:
    call_id = required_call_id(arguments)
    meeting = load_for_read(caller, call_id)
    settings = load_settings()
    items = [] if meeting.get("TranscriptDeletedAt") else read_boards(call_id)
    return {
        "CallId": call_id,
        "Items": [_answer(call_id, b, meeting) for b in items],
        "WhiteboardsEnabled": meeting.get("WhiteboardsEnabled") is True,
        "AssistantAvailable": settings.assistant_available,
        "MaxWhiteboards": settings.max_whiteboards,
        "Writable": not meeting.get("TranscriptDeletedAt"),
    }


def get_whiteboard(
    caller: Mapping[str, Any], arguments: Mapping[str, Any]
) -> Optional[Dict[str, Any]]:
    call_id = required_call_id(arguments)
    meeting = load_for_read(caller, call_id)
    whiteboard_id = rules_id(arguments.get("WhiteboardId"))
    version = arguments.get("Version")
    if version is None:
        board = read_board(call_id, whiteboard_id)
    else:
        board = _from_item(
            DYNAMODB.get_item(
                TableName=EVENT_SOURCING_TABLE,
                Key=_key(call_id, rules.version_key(whiteboard_id, int(version))),
            ).get("Item")
        )
    return _answer(call_id, board, meeting) if board else None


def _encode_token(key: Optional[Mapping[str, Any]]) -> Optional[str]:
    if not key:
        return None
    return base64.urlsafe_b64encode(json.dumps(key).encode("utf-8")).decode("ascii")


def _decode_token(token: Any, call_id: str, whiteboard_id: str) -> Optional[Dict[str, Any]]:
    if not token:
        return None
    try:
        key = json.loads(base64.urlsafe_b64decode(str(token).encode("ascii")))
        pk, sk = key["PK"]["S"], key["SK"]["S"]
    except (ValueError, KeyError, TypeError) as error:
        raise refuse("BadRequest", "NextToken is not one this query gave") from error
    # Only a key of this whiteboard's versions, so a token cannot reach other rows.
    if pk != rules.partition(call_id) or not sk.startswith(f"v#{whiteboard_id}#"):
        raise refuse("BadRequest", "NextToken is not one this query gave")
    return {"PK": {"S": pk}, "SK": {"S": sk}}


def list_versions(caller: Mapping[str, Any], arguments: Mapping[str, Any]) -> Dict[str, Any]:
    call_id = required_call_id(arguments)
    meeting = load_for_read(caller, call_id)
    whiteboard_id = rules_id(arguments.get("WhiteboardId"))
    try:
        limit = max(1, min(MAX_VERSION_PAGE, int(arguments.get("Limit") or 20)))
    except (TypeError, ValueError):
        limit = 20
    kwargs: Dict[str, Any] = {
        "TableName": EVENT_SOURCING_TABLE,
        "KeyConditionExpression": "PK = :pk AND begins_with(SK, :prefix)",
        "ExpressionAttributeValues": {
            ":pk": {"S": rules.partition(call_id)},
            ":prefix": {"S": f"v#{whiteboard_id}#"},
        },
        "ScanIndexForward": False,
        "Limit": limit,
        # The list without the panels; a version's panels are read on their own.
        "ProjectionExpression": "PK, SK, WhiteboardId, #v, Title, #s, ChangedPanels, UpdatedBy, UpdatedAt",
        "ExpressionAttributeNames": {"#s": "Source", "#v": "Version"},
    }
    start = _decode_token(arguments.get("NextToken"), call_id, whiteboard_id)
    if start:
        kwargs["ExclusiveStartKey"] = start
    response = DYNAMODB.query(**kwargs)
    items = _rows(response.get("Items"))
    return {
        "Items": [_answer(call_id, i, meeting) for i in items],
        "NextToken": _encode_token(response.get("LastEvaluatedKey")),
    }


def rules_id(value: Any) -> str:
    try:
        return rules.check_id(value, "WhiteboardId")
    except rules.WhiteboardError as error:
        raise _rules_error(error) from error


def create_whiteboard(caller: Mapping[str, Any], data: Mapping[str, Any]) -> Dict[str, Any]:
    call_id = required_call_id(data)
    meeting, check = load_for_write(caller, call_id)
    actor, source = actor_and_source(caller, data)
    settings = load_settings()
    try:
        board = rules.new_whiteboard(
            whiteboard_id=rules.new_id(),
            title=data.get("Title"),
            template=data.get("Template"),
            instructions=data.get("Instructions"),
            assistant_updates=data.get("AssistantUpdates"),
            panels=data.get("Panels"),
            source=source,
            actor=actor,
            now=now_iso(),
        )
    except rules.WhiteboardError as error:
        raise _rules_error(error) from error
    expires = expiry(meeting)
    count_write(call_id, expires)
    index_values: Dict[str, Any] = {
        ":one": {"N": "1"},
        ":max": {"N": str(settings.max_whiteboards)},
    }
    index_update = "ADD Boards :one"
    if expires:
        index_values[":ttl"] = {"N": str(expires)}
        index_update += " SET ExpiresAfter = if_not_exists(ExpiresAfter, :ttl)"
    reasons = _transact(
        [
            check,
            {
                "Update": {
                    "TableName": EVENT_SOURCING_TABLE,
                    "Key": _key(call_id, "index"),
                    "UpdateExpression": index_update,
                    "ConditionExpression": "attribute_not_exists(Boards) OR Boards < :max",
                    "ExpressionAttributeValues": index_values,
                }
            },
            {
                "Put": {
                    "TableName": EVENT_SOURCING_TABLE,
                    "Item": _head_row(call_id, board, expires),
                    "ConditionExpression": "attribute_not_exists(PK)",
                }
            },
            {
                "Put": {
                    "TableName": EVENT_SOURCING_TABLE,
                    "Item": _version_row(
                        call_id, board, source, [p["PanelId"] for p in board["Panels"]], expires
                    ),
                    "ConditionExpression": "attribute_not_exists(PK)",
                }
            },
        ]
    )
    if reasons:
        if reasons[0] == "ConditionalCheckFailed":
            raise _meeting_refusal(caller, call_id)
        if reasons[1] == "ConditionalCheckFailed":
            raise refuse(
                "WhiteboardLimit", f"A meeting has at most {settings.max_whiteboards} whiteboards"
            )
        raise refuse("WhiteboardConflict", "The whiteboard could not be stored; try again")
    LOGGER.info("Whiteboard %s created on %s by %s", board["WhiteboardId"], call_id, source)
    return _answer(call_id, board, meeting)


def update_whiteboard(caller: Mapping[str, Any], data: Mapping[str, Any]) -> Dict[str, Any]:
    call_id = required_call_id(data)
    meeting, check = load_for_write(caller, call_id)
    actor, source = actor_and_source(caller, data)
    whiteboard_id = rules_id(data.get("WhiteboardId"))
    board = read_board(call_id, whiteboard_id)
    if board is None:
        raise refuse("WhiteboardNotFound", "There is no such whiteboard on this meeting")
    try:
        base = int(data.get("BaseVersion"))  # type: ignore[arg-type]  # None: TypeError, refused
    except (TypeError, ValueError) as error:
        raise refuse("BadRequest", "BaseVersion is required") from error
    if base != board.get("Version"):
        raise refuse(
            "WhiteboardConflict",
            f"The whiteboard has changed since version {base}; it is at version {board.get('Version')}",
        )
    now = now_iso()
    try:
        if data.get("RestoreVersion") is not None:
            if source != rules.SOURCE_USER:
                raise rules.WhiteboardError("Only a user restores a version", "WhiteboardUserPanel")
            snapshot = _from_item(
                DYNAMODB.get_item(
                    TableName=EVENT_SOURCING_TABLE,
                    Key=_key(
                        call_id, rules.version_key(whiteboard_id, int(data["RestoreVersion"]))
                    ),
                ).get("Item")
            )
            if snapshot is None:
                raise rules.WhiteboardError("There is no such version", "WhiteboardNotFound")
            change = rules.restore(board, snapshot, actor=actor, now=now)
        else:
            change = rules.apply_change(
                board,
                source=source,
                actor=actor,
                now=now,
                title=data.get("Title"),
                instructions=data.get("Instructions"),
                assistant_updates=data.get("AssistantUpdates"),
                panels=data.get("Panels"),
            )
    except rules.WhiteboardError as error:
        raise _rules_error(error) from error
    if not change.changed:
        return _answer(call_id, board, meeting)
    expires = expiry(meeting)
    count_write(call_id, expires)
    # Every version expires with its whiteboard, which keeps the expiry it was
    # given when it was made, so no version outlives the whiteboard it belongs to.
    board_expiry = board.get("ExpiresAfter") or expires
    reasons = _transact(
        [
            check,
            {
                "Put": {
                    "TableName": EVENT_SOURCING_TABLE,
                    "Item": _head_row(call_id, change.board, board_expiry),
                    "ConditionExpression": "#v = :base",
                    "ExpressionAttributeNames": {"#v": "Version"},
                    "ExpressionAttributeValues": {":base": {"N": str(base)}},
                }
            },
            {
                "Put": {
                    "TableName": EVENT_SOURCING_TABLE,
                    "Item": _version_row(
                        call_id, change.board, source, change.changed_panels, board_expiry
                    ),
                    "ConditionExpression": "attribute_not_exists(PK)",
                }
            },
        ]
    )
    if reasons:
        if reasons[0] == "ConditionalCheckFailed":
            raise _meeting_refusal(caller, call_id)
        if read_board(call_id, whiteboard_id) is None:
            raise refuse("WhiteboardNotFound", "The whiteboard was deleted")
        raise refuse("WhiteboardConflict", "The whiteboard changed while this change was made")
    return _answer(call_id, change.board, meeting)


def delete_whiteboard(caller: Mapping[str, Any], data: Mapping[str, Any]) -> Dict[str, Any]:
    call_id = required_call_id(data)
    meeting, check = load_for_write(caller, call_id)
    whiteboard_id = rules_id(data.get("WhiteboardId"))
    board = read_board(call_id, whiteboard_id)
    if board is None:
        raise refuse("WhiteboardNotFound", "There is no such whiteboard on this meeting")
    count_write(call_id, expiry(meeting))
    reasons = _transact(
        [
            check,
            {
                "Delete": {
                    "TableName": EVENT_SOURCING_TABLE,
                    "Key": _key(call_id, rules.head_key(whiteboard_id)),
                    "ConditionExpression": "attribute_exists(PK)",
                }
            },
            {
                "Update": {
                    "TableName": EVENT_SOURCING_TABLE,
                    "Key": _key(call_id, "index"),
                    "UpdateExpression": "ADD Boards :minus",
                    "ConditionExpression": "attribute_exists(Boards) AND Boards > :zero",
                    "ExpressionAttributeValues": {":minus": {"N": "-1"}, ":zero": {"N": "0"}},
                }
            },
        ]
    )
    if reasons:
        if reasons[0] == "ConditionalCheckFailed":
            raise _meeting_refusal(caller, call_id)
        raise refuse("WhiteboardNotFound", "The whiteboard was already deleted")
    delete_rows(call_id, f"v#{whiteboard_id}#")
    LOGGER.info("Whiteboard %s deleted from %s", whiteboard_id, call_id)
    return _answer(
        call_id,
        {
            "WhiteboardId": whiteboard_id,
            "Title": board.get("Title"),
            "Version": board.get("Version"),
            "Panels": [],
        },
        meeting,
        Deleted=True,
    )


def delete_rows(call_id: str, prefix: str) -> int:
    """Delete the rows of a meeting's whiteboards under one sort-key prefix."""
    keys = [
        {"PK": {"S": rules.partition(call_id)}, "SK": {"S": row["SK"]}}
        for row in query_partition(call_id, prefix, ProjectionExpression="PK, SK")
    ]
    for start in range(0, len(keys), 25):
        requests = [{"DeleteRequest": {"Key": key}} for key in keys[start : start + 25]]
        pending: Any = {EVENT_SOURCING_TABLE: requests}
        for _attempt in range(5):
            response = DYNAMODB.batch_write_item(RequestItems=pending)
            pending = response.get("UnprocessedItems") or {}
            if not pending:
                break
    return len(keys)


def update_call_whiteboards(caller: Mapping[str, Any], data: Mapping[str, Any]) -> Dict[str, Any]:
    """Turn the assistant's updates on or off for the meeting; the meeting row afterwards."""
    call_id = required_call_id(data)
    meeting, check = load_for_write(caller, call_id)
    del meeting
    enabled = data.get("WhiteboardsEnabled")
    if not isinstance(enabled, bool):
        raise refuse("BadRequest", "WhiteboardsEnabled must be true or false")
    condition = check["ConditionCheck"]
    try:
        result = DYNAMODB.update_item(
            TableName=EVENT_SOURCING_TABLE,
            Key=_meeting_key(call_id),
            UpdateExpression="SET #w = :w",
            ConditionExpression=condition["ConditionExpression"],
            ExpressionAttributeNames={
                **condition["ExpressionAttributeNames"],
                "#w": "WhiteboardsEnabled",
            },
            ExpressionAttributeValues={
                **condition.get("ExpressionAttributeValues", {}),
                ":w": {"BOOL": enabled},
            },
            ReturnValues="ALL_NEW",
        )
    except ClientError as error:
        if _conflict(error):
            raise _meeting_refusal(caller, call_id) from error
        raise
    return _from_item(result["Attributes"]) or {}


def request_update(caller: Mapping[str, Any], data: Mapping[str, Any]) -> Dict[str, Any]:
    """Update now: a run that does not wait for the interval, as the caller."""
    call_id = required_call_id(data)
    load_for_write(caller, call_id)
    result = run(call_id, trigger="request")
    if result.get("ran"):
        return {
            "Success": True,
            "Message": result.get("reason") or "Updated",
            "Updated": result.get("updated", 0),
        }
    return {"Success": False, "Message": result.get("reason") or "Not updated", "Updated": 0}


QUERIES = {
    "listMeetingWhiteboards": list_whiteboards,
    "getMeetingWhiteboard": get_whiteboard,
    "listMeetingWhiteboardVersions": list_versions,
}
MUTATIONS = {
    "createMeetingWhiteboard": create_whiteboard,
    "updateMeetingWhiteboard": update_whiteboard,
    "deleteMeetingWhiteboard": delete_whiteboard,
    "updateCallWhiteboards": update_call_whiteboards,
}


# ── the assistant's run ──────────────────────────────────────────────────────

_BOARD_FIELDS = """
    CallId WhiteboardId Title Template Instructions AssistantUpdates Version
    CreatedBy CreatedAt UpdatedBy UpdatedAt Deleted Owner SharedWith
    Panels { PanelId Title Kind Content Source UpdatedBy UpdatedAt }
"""
UPDATE_MUTATION = (
    "mutation UpdateMeetingWhiteboard($input: UpdateMeetingWhiteboardInput!) {\n"
    "  updateMeetingWhiteboard(input: $input) {" + _BOARD_FIELDS + "}\n}"
)
CREATE_MUTATION = (
    "mutation CreateMeetingWhiteboard($input: CreateMeetingWhiteboardInput!) {\n"
    "  createMeetingWhiteboard(input: $input) {" + _BOARD_FIELDS + "}\n}"
)

#: Triggers that need only one new segment rather than minNewSegments: a run
#: on request (which waits REQUEST_GAP_SECONDS after the previous run instead
#: of the interval) and the run at a meeting's end (which waits only for the
#: lease).
EAGER_TRIGGERS = ("request", "final")
#: The least time between a run on request and the run before it, so repeated
#: presses of Update now cannot use up a meeting's runs at most.
REQUEST_GAP_SECONDS = 30


def why_not(
    meeting: Optional[Mapping[str, Any]], settings: rules.Settings, trigger: str
) -> Optional[str]:
    """Why a run does not happen now, or None when it may."""
    if not settings.assistant_available:
        return "the assistant's whiteboard updates are turned off for this deployment"
    if meeting is None:
        return "no such meeting"
    if meeting.get("TranscriptDeletedAt"):
        return "the meeting's transcript is deleted"
    if meeting.get("WhiteboardsEnabled") is not True:
        return "the assistant's whiteboard updates are off for this meeting"
    if trigger == "auto" and meeting.get("Status") == "ENDED":
        return "the meeting has ended"
    return None


def model_for(settings: rules.Settings) -> str:
    if settings.model_id:
        return resolve_model_id(settings.model_id, region=BEDROCK_REGION)
    return resolve_model_id(
        read_setting(LANGUAGE_MODELS.name, "meetingAssistModelId", default=FALLBACK_MODEL_ID),
        region=BEDROCK_REGION,
    )


def call_model(
    call_id: str, model_id: str, system: list, messages: list
) -> Optional[Dict[str, Any]]:
    """One model call, metered against the meeting; the tool's input, or None."""
    caching = read_setting(MEETING_ASSIST.name, "promptCachingEnabled", default=True)
    sections = prompt_cache_sections(model_id) if caching else frozenset()
    point = {"cachePoint": {"type": "default"}}
    tools: List[Dict[str, Any]] = [updater.TOOL] + (
        [point] if PROMPT_CACHE_TOOLS in sections else []
    )
    if PROMPT_CACHE_SYSTEM in sections:
        system = system + [point]
    if PROMPT_CACHE_MESSAGES in sections and len(messages[0]["content"]) > 1:
        content = messages[0]["content"]
        messages = [{"role": "user", "content": [content[0], point, *content[1:]]}]
    request: Dict[str, Any] = {
        "modelId": model_id,
        "system": system,
        "messages": messages,
        # "auto" rather than naming the tool: the Claude 5 models refuse a forced
        # tool choice; an answer given as JSON text is read the same way.
        "toolConfig": {"tools": tools, "toolChoice": {"auto": {}}},
        "inferenceConfig": build_inference_config(model_id, RUN_MAX_TOKENS),
    }
    extra = additional_request_fields(model_id, "low")
    if extra:
        request["additionalModelRequestFields"] = extra
    response = bedrock().converse(**request)
    usage = response.get("usage") or {}
    METERING.record(
        call_id,
        METERING_CONTEXT,
        bedrock_service(model_id),
        bedrock_usage(usage),
        region=BEDROCK_REGION if BEDROCK_REGION != os.environ.get("AWS_REGION") else None,
    )
    # Sizes only, never content: the transcript and the whiteboards are meeting content.
    LOGGER.info(
        "Whiteboards model call for %s: input=%s output=%s cacheRead=%s stop=%s",
        call_id,
        usage.get("inputTokens"),
        usage.get("outputTokens"),
        usage.get("cacheReadInputTokens"),
        response.get("stopReason"),
    )
    return updater.tool_input(response)


def _segment_rows(call_id: str) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    start = None
    for _page in range(MAX_TRANSCRIPT_PAGES):
        kwargs: Dict[str, Any] = {
            "TableName": EVENT_SOURCING_TABLE,
            "KeyConditionExpression": "PK = :pk",
            "ExpressionAttributeValues": {":pk": {"S": f"trs#{call_id}"}},
        }
        if start:
            kwargs["ExclusiveStartKey"] = start
        response = DYNAMODB.query(**kwargs)
        items.extend(_rows(response.get("Items")))
        start = response.get("LastEvaluatedKey")
        if not start:
            break
    return items


def _frame_rows(call_id: str) -> List[Dict[str, Any]]:
    response = DYNAMODB.query(
        TableName=EVENT_SOURCING_TABLE,
        KeyConditionExpression="PK = :pk",
        ExpressionAttributeValues={":pk": {"S": f"kf#{call_id}"}},
        ProjectionExpression="SK, #t, SlideText, Title, ContentKind, DuplicateOf",
        ExpressionAttributeNames={"#t": "Timestamp"},
    )
    return _rows(response.get("Items"))


def write_plan(call_id: str, board: Mapping[str, Any], panels: List[Dict[str, Any]]) -> bool:
    """One whiteboard's change through AppSync, as the assistant.

    A conflict (a user changed the whiteboard meanwhile) or a refusal (a user
    took a panel meanwhile) is met once by reading the whiteboard again and
    applying the run's content to the panels still the assistant's.
    """
    for attempt in range(2):
        try:
            appsync_graphql(
                UPDATE_MUTATION,
                {
                    "input": {
                        "CallId": call_id,
                        "WhiteboardId": board["WhiteboardId"],
                        "BaseVersion": board["Version"],
                        "Panels": panels,
                        "Source": rules.SOURCE_ASSISTANT,
                        "UpdatedBy": rules.ASSISTANT_LABEL,
                    }
                },
            )
            return True
        except NotificationError as error:
            if attempt or error.error_type not in ("WhiteboardConflict", "WhiteboardUserPanel"):
                LOGGER.info(
                    "Whiteboard %s of %s not updated: %s",
                    board["WhiteboardId"],
                    call_id,
                    error.error_type,
                )
                return False
            fresh = read_board(call_id, board["WhiteboardId"])
            if fresh is None or fresh.get("AssistantUpdates") is False:
                return False
            again = reapply(fresh, board, panels)
            if again is None:
                return False
            panels = again
            board = fresh
    return False


def reapply(
    fresh: Mapping[str, Any], seen: Mapping[str, Any], wanted: List[Dict[str, Any]]
) -> Optional[List[Dict[str, Any]]]:
    """The run's content applied to the whiteboard as it is now: only to panels that
    are still the assistant's, with any panels the run added appended."""
    editable = set(rules.assistant_panels(fresh))
    wanted_by_id = {p.get("PanelId"): p for p in wanted if p.get("PanelId")}
    seen_ids = {p["PanelId"] for p in seen.get("Panels") or []}
    added = [p for p in wanted if not p.get("PanelId") or p.get("PanelId") not in seen_ids]
    panels = []
    changed = False
    for panel in fresh.get("Panels") or []:
        entry = {k: panel[k] for k in ("PanelId", "Title", "Kind", "Content") if k in panel}
        replacement = wanted_by_id.get(panel["PanelId"])
        if (
            replacement
            and panel["PanelId"] in editable
            and replacement.get("Content") != panel.get("Content")
        ):
            entry["Content"] = replacement.get("Content")
            changed = True
        panels.append(entry)
    for panel in added:
        if len(panels) < rules.MAX_PANELS:
            panels.append({k: panel[k] for k in ("Title", "Kind", "Content") if k in panel})
            changed = True
    return panels if changed else None


def ensure_default_board(call_id: str, boards: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """A meeting the assistant updates and that has no whiteboard gets the default one."""
    if boards:
        return boards
    try:
        data = appsync_graphql(
            CREATE_MUTATION,
            {
                "input": {
                    "CallId": call_id,
                    "Template": rules.DEFAULT_TEMPLATE,
                    "Source": rules.SOURCE_ASSISTANT,
                    "UpdatedBy": rules.ASSISTANT_LABEL,
                }
            },
        )
    except NotificationError as error:
        LOGGER.info("No default whiteboard for %s: %s", call_id, error.error_type)
        return read_boards(call_id)
    created = data.get("createMeetingWhiteboard")
    return [created] if created else read_boards(call_id)


def run(call_id: str, trigger: str = "auto") -> Dict[str, Any]:
    """One run of the assistant over a meeting's whiteboards."""
    settings = load_settings()
    meeting = read_meeting(call_id, full=True)
    reason = why_not(meeting, settings, trigger)
    if reason or meeting is None:
        return {"ran": False, "reason": reason}
    now = time.time()
    state = LIVE.claim(
        call_id,
        now=now,
        interval_seconds=(
            REQUEST_GAP_SECONDS if trigger == "request" else settings.interval_seconds
        ),
        max_runs=settings.max_runs_per_meeting,
        expires=expiry(meeting),
        # The run at the meeting's end waits for nothing but the lease.
        ignore_interval=trigger == "final",
    )
    if state is None:
        return {
            "ran": False,
            "reason": "another run holds the meeting, ran recently, or used its runs",
        }
    lease = state["LeaseUntil"]
    try:
        return _update(call_id, meeting, settings, state, now, trigger)
    except Exception:  # pylint: disable=broad-except
        LOGGER.exception("Whiteboards run failed for %s; the next one waits an interval", call_id)
        LIVE.release(call_id, lease, ran=True, now=now)
        return {"ran": False, "reason": "the run failed"}


def _update(  # pylint: disable=too-many-arguments,too-many-locals
    call_id: str,
    meeting: Mapping[str, Any],
    settings: rules.Settings,
    state: Mapping[str, Any],
    now: float,
    trigger: str,
) -> Dict[str, Any]:
    lease = state["LeaseUntil"]
    rows = _segment_rows(call_id)
    segments = updater.final_segments(
        rows, meeting.get("SpeakerIdentities"), meeting.get("CustomerPhoneNumber")
    )
    fresh = updater.new_segments(segments, state.get("LastSegmentEndTime"))
    needed = 1 if trigger in EAGER_TRIGGERS else settings.min_new_segments
    if len(fresh) < needed:
        LIVE.release(call_id, lease, ran=False, now=now)
        return {"ran": False, "reason": f"{len(fresh)} new transcript segments"}
    boards = read_boards(call_id)
    # The default whiteboard once per meeting: a meeting whose people have
    # deleted every whiteboard does not get it back at the next run.
    if not state.get("DefaultMade"):
        boards = ensure_default_board(call_id, boards)
    # A whiteboard whose panels are all a user's is still a target: the
    # assistant may add a panel to it.
    targets = [b for b in boards if b.get("AssistantUpdates") is not False]
    if not targets:
        LIVE.release(call_id, lease, ran=False, now=now, DefaultMade=1)
        return {"ran": False, "reason": "no whiteboard the assistant updates"}
    screen, screen_marker = updater.screen_block(_frame_rows(call_id), state.get("LastScreenTime"))
    system, messages = updater.build_request(
        prompt=settings.prompt,
        meeting=meeting,
        boards=targets,
        notes=str(state.get("Notes") or ""),
        transcript=updater.transcript_block(fresh),
        screen=screen,
        elapsed_seconds=max(s.end for s in segments),
    )
    answer = call_model(call_id, model_for(settings), system, messages)
    plan = updater.parse_answer(answer, targets)
    marker = {"LastSegmentEndTime": max(s.end for s in fresh), "LastScreenTime": screen_marker}
    if plan is None:
        LOGGER.warning("Whiteboards answer for %s was malformed; dropped", call_id)
        LIVE.release(call_id, lease, ran=True, now=now, DefaultMade=1, **marker)
        return {"ran": True, "reason": "malformed answer", "updated": 0}
    by_id = {b["WhiteboardId"]: b for b in targets}
    updated = sum(
        1 for wid, panels in plan.panels.items() if write_plan(call_id, by_id[wid], panels)
    )
    LIVE.release(
        call_id,
        lease,
        ran=True,
        now=now,
        Notes=plan.notes or str(state.get("Notes") or ""),
        DefaultMade=1,
        **marker,
    )
    LOGGER.info(
        "Whiteboards run for %s (%s): segments=%d whiteboards=%d updated=%d dropped=%d",
        call_id,
        trigger,
        len(fresh),
        len(targets),
        updated,
        plan.dropped,
    )
    return {"ran": True, "updated": updated, "dropped": plan.dropped}


# ── entry point ──────────────────────────────────────────────────────────────


def handler(event: Dict[str, Any], _context: Any) -> Any:
    """AppSync (meetingWhiteboards.request.vtl) or an asynchronous run ({CallId, Trigger})."""
    info = event.get("info")
    if not isinstance(info, Mapping):
        call_id = event.get("CallId")
        trigger = str(event.get("Trigger")) if event.get("Trigger") in ("auto", "final") else "auto"
        if not isinstance(call_id, str) or not call_id:
            return {"ran": False, "reason": "no CallId"}
        result = run(call_id, trigger)
        LOGGER.info("Whiteboards run for %s: %s", call_id, result.get("reason") or "done")
        return result
    field = str(info.get("fieldName") or "")
    arguments = event.get("arguments") or {}
    caller = caller_of(event)
    if not caller["is_backend"] and not caller["username"]:
        raise refuse("Unauthorized", "Unauthorized")
    if field in QUERIES:
        return QUERIES[field](caller, arguments)
    if field in MUTATIONS:
        return MUTATIONS[field](caller, arguments.get("input") or {})
    if field == "requestWhiteboardUpdate":
        return request_update(caller, arguments)
    raise refuse("BadRequest", f"unsupported field {field}")
