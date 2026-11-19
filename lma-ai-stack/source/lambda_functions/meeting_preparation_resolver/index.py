#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""AppSync resolver for a meeting's preparation: its labels, audience, meeting
context and checklist.

One function for five mutations on the meeting row (``c#<CallId>``), and for
the check the Virtual Participant create and scheduled-edit pipelines run on
the same four inputs before they store them on the Virtual Participant's row:

================================ =================================================
``updateCallMeetingContext``     Replaces the meeting context; records who and when.
``updateCallChecklist``          Replaces the checklist. An item already present
                                 keeps its status, evidence and note unless
                                 ``ResetStatuses`` is true.
``updateCallChecklistItem``      Changes one item's status, note or evidence.
``updateCallLabels``             Replaces the labels.
``updateCallAudience``           Sets or clears the audience.
``validateMeetingPreparation``   Not in the schema: the first function of the
                                 createVirtualParticipant and
                                 updateScheduledVirtualParticipant pipelines.
================================ =================================================

Who may write is the rule ``updateCallAgenda`` applies: an Admin, the meeting's
owner, a user it is shared with, or a backend role calling with IAM. The rule is
checked on the row as read and restated in the write's DynamoDB condition, so a
meeting re-owned or re-shared in between is judged as it is at the write. A
checklist write is also conditional on the checklist's version as read, and is
read and applied again on a conflict, so two writers never undo each other.

A signed-in user's change is always attributed to their own username, and a
status they set is recorded as ``StatusSource: user``. Only a backend role names
a ``StatusSource``, and only an ``assistant`` change is held to the rule that a
status a user set is changed by a user alone. What the rules are is decided in
``lma_meeting_preparation`` (the configuration layer). See
docs/meeting-context-and-checklist.md.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

import boto3
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.config import Config as BotoCoreConfig
from botocore.exceptions import ClientError

# pylint: disable=import-error
from lma_meeting_preparation import rules  # configuration layer

# pylint: enable=import-error

# Stdlib logging rather than Powertools: this function carries only the
# configuration layer, which has no third-party dependencies.
LOGGER = logging.getLogger()
LOGGER.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

CLIENT_CONFIG = BotoCoreConfig(retries={"mode": "adaptive", "max_attempts": 3})
EVENT_SOURCING_TABLE = os.environ.get("EVENT_SOURCING_TABLE", "")
ADMIN_GROUP = os.environ.get("ADMIN_GROUP", "Admin")
DYNAMODB = boto3.client("dynamodb", config=CLIENT_CONFIG)
SERIALIZER = TypeSerializer()
DESERIALIZER = TypeDeserializer()

#: What a backend role's change is attributed to when it names nobody.
BACKEND_ATTRIBUTION = "LMA"
MAX_ATTRIBUTION_CHARS = 128
WRITE_ATTEMPTS = 4


class BadRequestError(Exception):
    """An input that breaks a rule."""


class ForbiddenError(Exception):
    """The caller may not change this meeting."""


class ConflictError(Exception):
    """The checklist kept changing while it was being written."""


class StatusSetByUserError(Exception):
    """The assistant tried to change an item whose status a user set."""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# ── the caller and the meeting ──────────────────────────────────────────────


def caller_of(event: Mapping[str, Any]) -> Dict[str, Any]:
    """Who is asking, from the AppSync ``identity`` block only.

    A request signed with IAM by one of LMA's own roles carries an assumed-role
    ``userArn`` and no user-pool claims, and is the backend -- the rule
    updateCallAgenda.request.vtl and the other Lambda resolvers apply.
    """
    identity = event.get("identity") or {}
    claims = identity.get("claims") or {}
    groups = claims.get("cognito:groups") or identity.get("groups") or []
    if isinstance(groups, str):
        groups = [g.strip() for g in groups.split(",") if g.strip()]
    user_arn = str(identity.get("userArn") or "")
    is_backend = not claims and ":assumed-role/" in user_arn
    return {
        # An IAM identity's username is a role session, not a person: never
        # matched against Owner or SharedWith, and never written as who changed
        # something.
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


def _plain(value: Any) -> Any:
    """A deserialised DynamoDB value with numbers and sets in JSON form."""
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
    """A plain value ready for TypeSerializer: floats as Decimal, None kept."""
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, list):
        return [_dynamo(v) for v in value]
    if isinstance(value, dict):
        return {k: _dynamo(v) for k, v in value.items()}
    return value


def _value(value: Any) -> Dict[str, Any]:
    return SERIALIZER.serialize(_dynamo(value))


def _meeting_key(call_id: str) -> Dict[str, Any]:
    return {"PK": {"S": f"c#{call_id}"}, "SK": {"S": f"c#{call_id}"}}


def read_meeting(call_id: str) -> Optional[Dict[str, Any]]:
    """What the rules need of the meeting row, read consistently."""
    item = DYNAMODB.get_item(
        TableName=EVENT_SOURCING_TABLE,
        Key=_meeting_key(call_id),
        ConsistentRead=True,
        ProjectionExpression="CallId, #o, SharedWith, Checklist, ChecklistVersion, TranscriptDeletedAt",
        ExpressionAttributeNames={"#o": "Owner"},
    ).get("Item")
    if not item:
        return None
    return {key: _plain(DESERIALIZER.deserialize(value)) for key, value in item.items()}


def access_condition(
    caller: Mapping[str, Any], meeting: Mapping[str, Any]
) -> Tuple[str, Dict, Dict]:
    """The caller's right to write, as a DynamoDB condition restating the read.

    Refuses a caller with none. A meeting that does not exist gets the same
    answer as one the caller may not see, as in getCall; a backend role is told
    that it does not exist.
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
            # The exact text that was read, whose entries include the caller.
            return (
                "(#accessOwner = :accessCaller OR #accessShared = :accessSharedAsRead)",
                names,
                {":accessCaller": {"S": username}, ":accessSharedAsRead": {"S": shared}},
            )
        # A List or string set: `contains` compares whole elements.
        return (
            "(#accessOwner = :accessCaller OR contains(#accessShared, :accessCaller))",
            names,
            {":accessCaller": {"S": username}},
        )
    raise ForbiddenError("Unauthorized: you may not change this meeting")


def load(caller: Mapping[str, Any], call_id: str) -> Tuple[Dict[str, Any], Tuple[str, Dict, Dict]]:
    meeting = read_meeting(call_id)
    if meeting is None:
        if caller["is_backend"]:
            raise BadRequestError(f"There is no meeting {call_id}")
        raise ForbiddenError("Unauthorized: you may not change this meeting")
    return meeting, access_condition(caller, meeting)


def attribution(caller: Mapping[str, Any], data: Mapping[str, Any]) -> str:
    """Who a change is recorded as made by: a signed-in user's own username,
    or for a backend role the label it gives (``LMA`` when it gives none)."""
    if not caller["is_backend"]:
        return caller["username"]
    label = data.get("UpdatedBy")
    if isinstance(label, str) and label.strip():
        return label.strip()[:MAX_ATTRIBUTION_CHARS]
    return BACKEND_ATTRIBUTION


def write(
    call_id: str,
    access: Tuple[str, Dict, Dict],
    sets: Mapping[str, Any],
    removes: Tuple[str, ...] = (),
    extra_condition: Tuple[str, Dict, Dict] = ("", {}, {}),
) -> Dict[str, Any]:
    """One conditional update of the meeting row; the row as it is afterwards."""
    names: Dict[str, str] = {}
    values: Dict[str, Any] = {}
    parts = []
    for index, (name, value) in enumerate(sets.items()):
        names[f"#s{index}"] = name
        values[f":s{index}"] = _value(value)
        parts.append(f"#s{index} = :s{index}")
    expression = "SET " + ", ".join(parts)
    if removes:
        removed = []
        for index, name in enumerate(removes):
            names[f"#r{index}"] = name
            removed.append(f"#r{index}")
        expression += " REMOVE " + ", ".join(removed)
    conditions = ["attribute_exists(PK)"]
    for condition, condition_names, condition_values in (access, extra_condition):
        if condition:
            conditions.append(condition)
            names.update(condition_names)
            values.update(condition_values)
    result = DYNAMODB.update_item(
        TableName=EVENT_SOURCING_TABLE,
        Key=_meeting_key(call_id),
        UpdateExpression=expression,
        ConditionExpression=" AND ".join(conditions),
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
        ReturnValues="ALL_NEW",
    )
    return {
        key: _plain(DESERIALIZER.deserialize(value)) for key, value in result["Attributes"].items()
    }


def _conflict(error: ClientError) -> bool:
    return error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


def write_once(
    caller: Mapping[str, Any], call_id: str, sets: Mapping[str, Any], removes=()
) -> Dict:
    """A write that does not depend on what the row held: one attempt.

    A failed condition here means the caller's access went in between.
    """
    _meeting, access = load(caller, call_id)
    try:
        return write(call_id, access, sets, removes)
    except ClientError as error:
        if _conflict(error):
            raise ForbiddenError("Unauthorized: you may not change this meeting") from error
        raise


def write_checklist(
    caller: Mapping[str, Any],
    call_id: str,
    change: Callable[[List[Dict[str, Any]], Dict[str, Any], str], List[Dict[str, Any]]],
) -> Dict[str, Any]:
    """Read the checklist, apply ``change``, and write it on condition that it is
    still the version read; on a conflict, read and apply again."""
    for _attempt in range(WRITE_ATTEMPTS):
        meeting, access = load(caller, call_id)
        now = now_iso()
        try:
            items = change(list(meeting.get("Checklist") or []), meeting, now)
            rules.check_checklist_size(items)
        except rules.StatusSetByUser as error:
            raise StatusSetByUserError(str(error)) from error
        except rules.PreparationError as error:
            raise BadRequestError(str(error)) from error
        version = int(meeting.get("ChecklistVersion") or 0)
        if version:
            guard = (
                "#guardVersion = :guardVersion",
                {"#guardVersion": "ChecklistVersion"},
                {":guardVersion": {"N": str(version)}},
            )
        else:
            guard = (
                "attribute_not_exists(#guardVersion)",
                {"#guardVersion": "ChecklistVersion"},
                {},
            )
        try:
            row = write(
                call_id,
                access,
                {"Checklist": items, "ChecklistUpdatedAt": now, "ChecklistVersion": version + 1},
                extra_condition=guard,
            )
        except ClientError as error:
            if _conflict(error):
                LOGGER.info("the checklist of %s changed while writing; reading it again", call_id)
                continue
            raise
        LOGGER.info("checklist of %s: version %d, %d items", call_id, version + 1, len(items))
        return row
    raise ConflictError("The checklist kept changing; try again")


def _call_id(data: Mapping[str, Any]) -> str:
    call_id = data.get("CallId")
    if not isinstance(call_id, str) or not call_id:
        raise BadRequestError("CallId is required")
    return call_id


def _checked(function: Callable, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except rules.PreparationError as error:
        raise BadRequestError(str(error)) from error


# ── the mutations ───────────────────────────────────────────────────────────


def update_meeting_context(caller: Mapping[str, Any], data: Mapping[str, Any]) -> Dict:
    context = _checked(rules.normalize_context, data.get("MeetingContext"))
    return write_once(
        caller,
        _call_id(data),
        {
            "MeetingContext": context,
            "MeetingContextUpdatedBy": attribution(caller, data),
            "MeetingContextUpdatedAt": now_iso(),
        },
    )


def update_labels(caller: Mapping[str, Any], data: Mapping[str, Any]) -> Dict:
    labels = _checked(rules.normalize_labels, data.get("Labels"))
    # Stored as [] when cleared, so a reader can tell "no labels" from a
    # meeting written before labels existed.
    return write_once(
        caller, _call_id(data), {"Labels": labels, "MeetingMetadataUpdatedAt": now_iso()}
    )


def update_audience(caller: Mapping[str, Any], data: Mapping[str, Any]) -> Dict:
    audience = _checked(rules.normalize_audience, data.get("Audience"))
    if audience is None:
        return write_once(
            caller, _call_id(data), {"MeetingMetadataUpdatedAt": now_iso()}, ("Audience",)
        )
    return write_once(
        caller, _call_id(data), {"Audience": audience, "MeetingMetadataUpdatedAt": now_iso()}
    )


def update_checklist(caller: Mapping[str, Any], data: Mapping[str, Any]) -> Dict:
    definitions = _checked(rules.normalize_definitions, data.get("Checklist"))
    by = attribution(caller, data)
    reset = data.get("ResetStatuses") is True

    def change(items, _meeting, now):
        return rules.merge_checklist(
            items, definitions, reset_statuses=reset, updated_by=by, now=now
        )

    return write_checklist(caller, _call_id(data), change)


def update_checklist_item(caller: Mapping[str, Any], data: Mapping[str, Any]) -> Dict:
    if caller["is_backend"]:
        source = data.get("StatusSource")
        if source not in rules.STATUS_SOURCES:
            raise BadRequestError("StatusSource must be user or assistant")
    else:
        # A signed-in user acts for themselves, whatever the request says.
        source = rules.STATUS_SOURCE_USER
    by = attribution(caller, data)

    def change(items, meeting, now):
        updated, _item = rules.apply_item_update(
            items,
            data,
            status_source=source,
            updated_by=by,
            now=now,
            # A quote is transcript content: none is stored once the
            # transcript has been deleted.
            quotes_allowed=not meeting.get("TranscriptDeletedAt"),
        )
        return updated

    return write_checklist(caller, _call_id(data), change)


# ── the Virtual Participant pipelines ───────────────────────────────────────

#: The four inputs as createVirtualParticipant and updateScheduledVirtualParticipant
#: name them, which are also their names on the Virtual Participant's row.
VP_FIELDS = ("labels", "audience", "meetingContext", "checklist")


def validate_meeting_preparation(arguments: Mapping[str, Any]) -> Dict[str, Any]:
    """The four inputs of a Virtual Participant, checked and normalized.

    ``present`` lists the inputs the request carried; for each, the value to
    store, or None when the input clears it (an empty list or text). The
    pipeline's next function stores or removes them; nothing is written here.
    The checklist is stored as its definitions: every item starts open when
    the Virtual Participant creates the meeting.
    """
    data = arguments.get("input") or {}
    present = [name for name in arguments.get("present") or [] if name in VP_FIELDS]
    out: Dict[str, Any] = {"present": present}
    try:
        if "labels" in present:
            out["labels"] = rules.normalize_labels(data.get("labels")) or None
        if "audience" in present:
            out["audience"] = rules.normalize_audience(data.get("audience"))
        if "meetingContext" in present:
            out["meetingContext"] = rules.normalize_context(data.get("meetingContext")) or None
        if "checklist" in present:
            checklist = rules.normalize_definitions(data.get("checklist"))
            rules.check_checklist_size(checklist)
            out["checklist"] = checklist or None
    except rules.PreparationError as error:
        raise BadRequestError(str(error)) from error
    return out


MUTATIONS = {
    "updateCallMeetingContext": update_meeting_context,
    "updateCallChecklist": update_checklist,
    "updateCallChecklistItem": update_checklist_item,
    "updateCallLabels": update_labels,
    "updateCallAudience": update_audience,
}


def handler(event: Dict[str, Any], _context: Any) -> Dict[str, Any]:
    """AppSync Lambda resolver entry point (meetingPreparation.request.vtl payload)."""
    field = ((event.get("info") or {}).get("fieldName")) or ""
    arguments = event.get("arguments") or {}
    if field == "validateMeetingPreparation":
        return validate_meeting_preparation(arguments)
    operation = MUTATIONS.get(field)
    if operation is None:
        raise BadRequestError(f"unsupported field {field}")
    caller = caller_of(event)
    if not caller["is_backend"] and not caller["username"]:
        raise ForbiddenError("Unauthorized: you may not change this meeting")
    return operation(caller, arguments.get("input") or {})
