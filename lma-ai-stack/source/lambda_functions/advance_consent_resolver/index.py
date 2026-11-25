#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""AppSync resolver for consent given in advance, by the signed-in person themselves.

Five operations, each admitted to every signed-in role (Admin, User and
Participant) and each acting on the caller's own records only:

=================================== ==============================================
``listMyAdvanceConsents``           The caller's consents given in advance.
``getAdvanceConsentInvitation``     A meeting's consent page, as the caller sees
                                    it: whether they are on its participant list
                                    and, when they are, the meeting, the text they
                                    would agree to for the scope asked for, and
                                    their consents that cover it. Someone not on
                                    the list is told only that.
``giveAdvanceConsent``              A consent for the meeting, or for the meetings
                                    its organizer holds until a date. Only for a
                                    person on the meeting's list, and only for the
                                    text the page showed (by its hash).
``withdrawAdvanceConsent``          Withdraws one of the caller's consents.
``askToBeAddedToMeeting``           For a signed-in person not on the list: shows,
                                    in LMA, the people who see the meeting's
                                    participant list that they asked to be added.
                                    Nothing about consent is recorded.
=================================== ==============================================

Who the caller is comes from the verified AppSync identity only: the username
and Cognito sub of the token, and the email address the user pool holds for
that username, which must be verified. No argument names a person. A
consent is stored under the caller's email address (``PK = pmc#<address>``,
compared ignoring case), so reading or withdrawing one names only the caller's
own partition: another person's consent id is not found there.

The rules -- scopes, periods, text, what a consent covers -- are in
``lma_consent.advance`` in the configuration layer. See
docs/meeting-consent.md#consent-given-in-advance.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Mapping, Optional

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.config import Config as BotoCoreConfig
from botocore.exceptions import BotoCoreError, ClientError

# pylint: disable=import-error
from lma_config import (  # configuration layer
    MEETING_CONSENT,
    ConfigurationError,
    ConfigurationStore,
    ConfigurationTableNotConfigured,
    offered_settings,
)
from lma_consent import advance  # configuration layer
from lma_consent import rules as consent  # configuration layer

# pylint: enable=import-error

# Stdlib logging: this function carries only the configuration layer.
LOGGER = logging.getLogger()
LOGGER.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

CLIENT_CONFIG = BotoCoreConfig(retries={"mode": "adaptive", "max_attempts": 3})
EVENT_SOURCING_TABLE = os.environ.get("EVENT_SOURCING_TABLE", "")
VIRTUAL_PARTICIPANT_TABLE = os.environ.get("VIRTUAL_PARTICIPANT_TABLE", "")
USER_POOL_ID = os.environ.get("USER_POOL_ID", "")
#: The meeting consent resolver, invoked for each meeting in progress that
#: counted a consent being withdrawn.
MEETING_CONSENT_FUNCTION = os.environ.get("MEETING_CONSENT_FUNCTION", "")
#: How long a consent stays stored after it stops counting (withdrawn or
#: expired): as long as the meeting records it may have been used in.
MEETING_RECORD_EXPIRATION_DAYS = float(os.environ.get("MEETING_RECORD_EXPIRATION_IN_DAYS", "90"))

#: The most consents of one person read back, newest first.
MAX_CONSENTS_PER_PERSON = 200
#: The most consents one person may have counting at once; a give beyond it is refused.
MAX_ACTIVE_PER_PERSON = 100
#: The most times one person may ask to be added to meetings' lists in a day (UTC).
ASKS_PER_DAY = 20
#: The most requests kept on one Virtual Participant's row.
MAX_REQUESTS_PER_VP = 20
#: How long a request to be added is remembered, so asking again does not notify again.
REQUEST_SECONDS = 30 * 24 * 3600
MAX_ID_LENGTH = 512
#: The most meetings a withdrawal is passed on to.
MAX_WITHDRAWAL_MEETINGS = 50

DYNAMODB = boto3.resource("dynamodb", config=CLIENT_CONFIG)
COGNITO = boto3.client("cognito-idp", config=CLIENT_CONFIG)
LAMBDA = boto3.client("lambda", config=CLIENT_CONFIG)

_STORE: Optional[ConfigurationStore] = None
_MARKDOWN_SPECIAL = re.compile(r"([\\`*_{}\[\]()#+\-.!|<>~])")


class Refused(Exception):
    """The caller may not do what they asked."""


class BadRequest(Exception):
    """The request is malformed; the message says how."""


# ── who is asking ───────────────────────────────────────────────────────────


def caller_of(event: Mapping[str, Any]) -> Dict[str, Any]:
    """The signed-in person, from the AppSync identity only.

    A request signed with IAM carries no user-pool username and is refused:
    these operations are a person's own, and a backend role has none.
    """
    identity = event.get("identity") or {}
    claims = identity.get("claims") or {}
    username = identity.get("username") or claims.get("cognito:username") or ""
    sub = claims.get("sub") or identity.get("sub") or ""
    if not isinstance(username, str) or not username or not sub:
        raise Refused("these operations are for a signed-in person")
    source_ip = identity.get("sourceIp")
    if isinstance(source_ip, list):
        source_ip = source_ip[0] if source_ip else ""
    request = event.get("request") or {}
    return {
        "username": username,
        "sub": str(sub),
        "source_ip": str(source_ip or "")[:64],
        "user_agent": str(request.get("userAgent") or "")[:512],
    }


def account_email(caller: Mapping[str, Any]) -> str:
    """The caller's verified email address, as the user pool holds it, in lower case.

    Read from the user pool by the token's username, and only when its sub is
    the token's, so an address changed or an account replaced since the token
    was issued is not used.
    """
    try:
        user = COGNITO.admin_get_user(UserPoolId=USER_POOL_ID, Username=caller["username"])
    except COGNITO.exceptions.UserNotFoundException as error:
        raise Refused("your account was not found") from error
    attributes = {a["Name"]: a.get("Value") for a in user.get("UserAttributes") or []}
    if attributes.get("sub") != caller["sub"]:
        raise Refused("your account was not found")
    email = (attributes.get("email") or "").strip()
    if not email or str(attributes.get("email_verified")).lower() != "true":
        raise Refused("your account has no verified email address")
    return email.casefold()


# ── settings ────────────────────────────────────────────────────────────────


def consent_settings() -> Dict[str, Any]:
    """The Meeting Consent section, read now (its tier is immediate)."""
    global _STORE  # pylint: disable=global-statement
    try:
        if _STORE is None:
            _STORE = ConfigurationStore()
        values = _STORE.get_section(MEETING_CONSENT.name).values
    except ConfigurationTableNotConfigured:
        LOGGER.warning("CONFIGURATION_TABLE_NAME is unset; using the shipped consent defaults")
        values = {}
    except (ClientError, BotoCoreError, ConfigurationError) as error:
        LOGGER.exception("could not read the meeting consent settings")
        raise BadRequest("the meeting consent settings could not be read; try again") from error
    return consent.effective_settings(offered_settings(MEETING_CONSENT.name, values))


# ── the meeting a link names ────────────────────────────────────────────────


def _plain(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    return value


def _participants(value: Any) -> List[Dict[str, str]]:
    out = []
    for entry in value or []:
        if isinstance(entry, Mapping):
            email = entry.get("email")
            if isinstance(email, str) and email.strip():
                out.append({"name": str(entry.get("name") or ""), "email": email.strip()})
    return out


def _labels(value: Any) -> List[Dict[str, str]]:
    out = []
    for entry in value or []:
        if isinstance(entry, Mapping):
            key, label = (
                entry.get("Key") or entry.get("key"),
                entry.get("Value") or entry.get("value"),
            )
            if isinstance(key, str) and isinstance(label, str):
                out.append({"Key": key, "Value": label})
    return out


def _meeting_row(call_id: str) -> Optional[Dict[str, Any]]:
    item = (
        DYNAMODB.Table(EVENT_SOURCING_TABLE)
        .get_item(
            Key={"PK": f"c#{call_id}", "SK": f"c#{call_id}"},
            ProjectionExpression="CallId, #o, Participants, Labels, CreatedAt",
            ExpressionAttributeNames={"#o": "Owner"},
        )
        .get("Item")
    )
    return _plain(item) if item else None


def _vp_row(vp_id: str) -> Optional[Dict[str, Any]]:
    item = (
        DYNAMODB.Table(VIRTUAL_PARTICIPANT_TABLE)
        .get_item(
            Key={"id": vp_id},
            ProjectionExpression=(
                "id, #o, meetingName, participants, labels, scheduledFor, CallId, "
                "participantListRequests"
            ),
            ExpressionAttributeNames={"#o": "Owner"},
        )
        .get("Item")
    )
    return _plain(item) if item else None


def _ref_id(value: Any, name: str) -> str:
    if value is None or value == "":
        return ""
    if not isinstance(value, str) or len(value) > MAX_ID_LENGTH:
        raise BadRequest(f"{name} must be at most {MAX_ID_LENGTH} characters")
    return value


def meeting_of(arguments: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """The meeting a consent link names, or None when there is none.

    Named by ``CallId`` or, for a Virtual Participant that may not have created
    its meeting yet, by ``VirtualParticipantId`` (exactly one). A Virtual
    Participant that has created its meeting is read as that meeting, whose
    participant list is the one its owner edits from then on, keeping the
    Virtual Participant's id and meeting name.
    """
    call_id = _ref_id(arguments.get("CallId"), "CallId")
    vp_id = _ref_id(arguments.get("VirtualParticipantId"), "VirtualParticipantId")
    if bool(call_id) == bool(vp_id):
        raise BadRequest("Name the meeting with exactly one of CallId and VirtualParticipantId")
    if call_id:
        row = _meeting_row(call_id)
        if not row:
            return None
        return {
            "Kind": "call",
            "CallId": call_id,
            "VirtualParticipantId": "",
            "Owner": row.get("Owner") or "",
            "Name": call_id,
            "Participants": _participants(row.get("Participants")),
            "Labels": _labels(row.get("Labels")),
            "ScheduledFor": None,
        }
    vp = _vp_row(vp_id)
    if not vp:
        return None
    meeting = {
        "Kind": "vp",
        "CallId": "",
        "VirtualParticipantId": vp_id,
        "Owner": vp.get("Owner") or "",
        "Name": str(vp.get("meetingName") or "") or vp_id,
        "Participants": _participants(vp.get("participants")),
        "Labels": _labels(vp.get("labels")),
        "ScheduledFor": vp.get("scheduledFor") or None,
    }
    linked = vp.get("CallId")
    row = _meeting_row(linked) if isinstance(linked, str) and linked else None
    if row and (row.get("Owner") or "") == meeting["Owner"]:
        meeting.update(
            {
                "Kind": "call",
                "CallId": linked,
                "Participants": _participants(row.get("Participants")),
                "Labels": _labels(row.get("Labels")),
            }
        )
    return meeting


def listed_entry(meeting: Mapping[str, Any], email: str) -> Optional[Dict[str, str]]:
    """The caller's entry on the meeting's participant list, compared ignoring case."""
    for person in meeting.get("Participants") or []:
        if person["email"].casefold() == email:
            return person
    return None


# ── the store ───────────────────────────────────────────────────────────────


def _partition(email: str) -> str:
    return f"pmc#{email}"


def _table():
    return DYNAMODB.Table(EVENT_SOURCING_TABLE)


def my_consents(email: str, not_withdrawn: bool = False) -> List[Dict[str, Any]]:
    """The caller's consents, newest first: the newest MAX_CONSENTS_PER_PERSON,
    or with ``not_withdrawn`` every one not withdrawn, however old, read a page
    at a time (each person has at most MAX_ACTIVE_PER_PERSON in force)."""
    arguments: Dict[str, Any] = {
        "KeyConditionExpression": Key("PK").eq(_partition(email)) & Key("SK").begins_with("ac#"),
        "ScanIndexForward": False,
        "ConsistentRead": True,
    }
    if not_withdrawn:
        arguments["FilterExpression"] = Attr("WithdrawnAt").not_exists()
    else:
        arguments["Limit"] = MAX_CONSENTS_PER_PERSON
    items: List[Dict[str, Any]] = []
    while True:
        response = _table().query(**arguments)
        items.extend(_plain(item) for item in response.get("Items") or [])
        if not not_withdrawn or not response.get("LastEvaluatedKey"):
            return items
        arguments["ExclusiveStartKey"] = response["LastEvaluatedKey"]


def _expires_after(stops_counting: datetime) -> int:
    return int(stops_counting.timestamp() + MEETING_RECORD_EXPIRATION_DAYS * 86400)


def consent_out(item: Mapping[str, Any], now: datetime) -> Dict[str, Any]:
    """The ``AdvanceConsent`` GraphQL shape: what the person agreed to, never their device."""
    fields = (
        "ConsentId",
        "Scope",
        "CallId",
        "VirtualParticipantId",
        "MeetingName",
        "Organizer",
        "LabelKey",
        "LabelValue",
        "GrantedAt",
        "ValidUntil",
        "WithdrawnAt",
        "WithdrawnReason",
        "NoticeText",
        "NoticeHash",
        "NoticeVersion",
        "Source",
        "Email",
    )
    out = {name: item.get(name) for name in fields}
    out["Status"] = advance.status_of(item, now)
    out["DeviceDetailsKept"] = bool(item.get("DeviceDetails"))
    return out


def _new_consent_id(now: datetime) -> str:
    """Sortable by when it was given, so the newest are read first."""
    return f"{now.strftime('%Y%m%d%H%M%S')}-{secrets.token_hex(6)}"


def _withdraw(email: str, consent_id: str, now: datetime, reason: str) -> Optional[Dict]:
    """Mark one consent withdrawn; None when it is not the caller's, already
    withdrawn, or expired."""
    try:
        response = _table().update_item(
            Key={"PK": _partition(email), "SK": f"ac#{consent_id}"},
            UpdateExpression="SET WithdrawnAt = :w, WithdrawnReason = :r, ExpiresAfter = :e",
            # Only one still in force: an expired consent stopped counting at
            # its end date, and keeps the expiry counted from then.
            ConditionExpression=(
                "attribute_exists(PK) AND attribute_not_exists(WithdrawnAt) AND ValidUntil > :w"
            ),
            ExpressionAttributeValues={
                ":w": advance.iso(now),
                ":r": reason,
                ":e": _expires_after(now),
            },
            ReturnValues="ALL_NEW",
        )
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return None
        raise
    return _plain(response.get("Attributes") or {})


# ── what the person would agree to ──────────────────────────────────────────


def _label_of(meeting: Mapping[str, Any], key: Any) -> Optional[tuple]:
    """The meeting's label with ``key``, as ``(key, value)``; None when ``key`` is empty."""
    if not key:
        return None
    for label in meeting.get("Labels") or []:
        if label["Key"] == key:
            return (label["Key"], label["Value"])
    raise BadRequest("LabelKey must be one of the meeting's labels")


def proposed(
    meeting: Mapping[str, Any], arguments: Mapping[str, Any], settings: Mapping[str, Any], now
) -> Dict[str, Any]:
    """The consent the arguments describe, with the exact text it would be given with."""
    scope = arguments.get("Scope") or advance.SCOPE_MEETING
    if scope not in advance.SCOPES:
        raise BadRequest(f"Scope must be one of {', '.join(advance.SCOPES)}")
    days = advance.max_days(settings.get("advanceConsentMaxDays"))
    try:
        until = advance.valid_until(scope, arguments.get("ValidUntil"), now, days)
    except advance.InvalidConsent as error:
        raise BadRequest(str(error)) from error
    label = _label_of(meeting, arguments.get("LabelKey")) if scope == "organizer" else None
    template = settings.get("advanceConsentText")
    text = advance.render_text(
        template,
        organizer=meeting["Owner"],
        meeting_name=meeting["Name"],
        scope=advance.scope_phrase(
            scope,
            meeting_name=meeting["Name"],
            until=until if scope == advance.SCOPE_ORGANIZER else None,
            label=label,
        ),
    )
    return {
        "Scope": scope,
        "ValidUntil": advance.iso(until),
        "LabelKey": label[0] if label else None,
        "LabelValue": label[1] if label else None,
        "NoticeText": text,
        "NoticeHash": advance.text_hash(text),
        "NoticeVersion": advance.text_version(template),
        "MaxDays": days,
    }


def _covering_meeting(meeting: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "CallId": meeting.get("CallId"),
        "VirtualParticipantId": meeting.get("VirtualParticipantId"),
        "Owner": meeting.get("Owner"),
        "Labels": meeting.get("Labels"),
    }


# ── the operations ──────────────────────────────────────────────────────────


def list_my_advance_consents(caller: Mapping[str, Any], _arguments: Mapping[str, Any]) -> Dict:
    email = account_email(caller)
    settings = consent_settings()
    now = datetime.now(timezone.utc)
    return {
        "Consents": [consent_out(item, now) for item in my_consents(email)],
        "Enabled": bool(settings.get("advanceConsentEnabled")),
        "MaxDays": advance.max_days(settings.get("advanceConsentMaxDays")),
        "Email": email,
    }


def _request_key(meeting: Optional[Mapping[str, Any]], arguments: Mapping[str, Any]) -> str:
    """Where a request to be added is remembered: under the meeting, or, for a
    link to no meeting, under the id the link names, so that both are answered
    alike."""
    if meeting is None:
        call_id = _ref_id(arguments.get("CallId"), "CallId")
        if call_id:
            return f"pmr#c#{call_id}"
        return f"pmr#vp#{_ref_id(arguments.get('VirtualParticipantId'), 'VirtualParticipantId')}"
    if meeting["Kind"] == "call":
        return f"pmr#c#{meeting['CallId']}"
    return f"pmr#vp#{meeting['VirtualParticipantId']}"


def _already_requested(key: str, email: str) -> bool:
    item = _table().get_item(Key={"PK": key, "SK": email}).get("Item")
    return bool(item) and int(item.get("ExpiresAfter") or 0) > time.time()


def _with_created_meetings(consents: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The consents, each one for a Virtual Participant's meeting also naming that meeting.

    A consent for one meeting given from a Virtual Participant's link before
    it created its meeting names only the Virtual Participant. Once it has
    created the meeting, the meeting's own link names it by CallId; the
    Virtual Participant's row says which, so such a consent is read as one for
    that CallId too (in memory only), and covers and is replaced as one given
    from the meeting's link.
    """
    created: Dict[str, str] = {}
    out = []
    for item in consents:
        vp_id = item.get("VirtualParticipantId")
        if item.get("Scope") == advance.SCOPE_MEETING and vp_id and not item.get("CallId"):
            if vp_id not in created:
                row = _vp_row(str(vp_id)) or {}
                created[vp_id] = str(row.get("CallId") or "")
            if created[vp_id]:
                item = {**item, "CallId": created[vp_id]}
        out.append(item)
    return out


def get_advance_consent_invitation(caller: Mapping[str, Any], arguments: Mapping[str, Any]) -> Dict:
    email = account_email(caller)
    settings = consent_settings()
    enabled = bool(settings.get("advanceConsentEnabled"))
    meeting = meeting_of(arguments)
    entry = listed_entry(meeting, email) if meeting else None
    if meeting is None or entry is None:
        # Someone not on the list, or a link to no meeting, learns nothing
        # about the meeting: not its name, its owner nor who is listed.
        requested = _already_requested(_request_key(meeting, arguments), email)
        LOGGER.info("consent page opened by someone not on the meeting's list")
        return {"Listed": False, "Requested": requested, "Enabled": enabled, "Email": email}
    now = datetime.now(timezone.utc)
    offer = proposed(meeting, arguments, settings, now) if enabled else {}
    covering = [
        consent_out(item, now)
        for item in _with_created_meetings(my_consents(email, not_withdrawn=True))
        if advance.covers(item, _covering_meeting(meeting), now)
    ]
    return {
        "Listed": True,
        "Requested": False,
        "Enabled": enabled,
        "Email": email,
        "ListedName": entry["name"] or None,
        "CallId": meeting["CallId"] or None,
        "VirtualParticipantId": meeting["VirtualParticipantId"] or None,
        "MeetingName": meeting["Name"],
        "Organizer": meeting["Owner"],
        "ScheduledFor": meeting["ScheduledFor"],
        "Labels": meeting["Labels"],
        "Scope": offer.get("Scope"),
        "ValidUntil": offer.get("ValidUntil"),
        "LabelKey": offer.get("LabelKey"),
        "LabelValue": offer.get("LabelValue"),
        "NoticeText": offer.get("NoticeText"),
        "NoticeHash": offer.get("NoticeHash"),
        "MaxDays": offer.get("MaxDays"),
        "ActiveConsents": covering,
    }


def give_advance_consent(caller: Mapping[str, Any], data: Mapping[str, Any]) -> Dict:
    email = account_email(caller)
    settings = consent_settings()
    if not settings.get("advanceConsentEnabled"):
        raise Refused("this deployment does not take consent given in advance")
    meeting = meeting_of(data)
    if not meeting or listed_entry(meeting, email) is None:
        raise Refused("you are not on this meeting's participant list")
    if not meeting["Owner"]:
        raise BadRequest("the meeting has no owner")
    now = datetime.now(timezone.utc)
    offer = proposed(meeting, data, settings, now)
    if data.get("NoticeHash") != offer["NoticeHash"]:
        # What the page showed is not what would be stored: the settings, the
        # meeting or the date changed since. Nothing is recorded.
        raise BadRequest("the consent text has changed since the page showed it; read it again")
    active = [
        item
        for item in _with_created_meetings(my_consents(email, not_withdrawn=True))
        if advance.status_of(item, now) == advance.STATUS_ACTIVE
    ]
    if len(active) >= MAX_ACTIVE_PER_PERSON:
        raise BadRequest(f"you have {MAX_ACTIVE_PER_PERSON} consents in force; withdraw one first")
    until = advance.parse_time(offer["ValidUntil"]) or now
    consent_id = _new_consent_id(now)
    item: Dict[str, Any] = {
        "PK": _partition(email),
        "SK": f"ac#{consent_id}",
        "ConsentId": consent_id,
        "Email": email,
        "Subject": caller["sub"],
        "Username": caller["username"],
        "Source": advance.SOURCE_PRE_MEETING,
        "Scope": offer["Scope"],
        "Organizer": meeting["Owner"],
        "MeetingName": meeting["Name"],
        "GrantedAt": advance.iso(now),
        "ValidUntil": offer["ValidUntil"],
        "NoticeText": offer["NoticeText"],
        "NoticeHash": offer["NoticeHash"],
        "NoticeVersion": offer["NoticeVersion"],
        "ExpiresAfter": _expires_after(until),
    }
    if offer["Scope"] == advance.SCOPE_MEETING:
        if meeting["CallId"]:
            item["CallId"] = meeting["CallId"]
        if meeting["VirtualParticipantId"]:
            item["VirtualParticipantId"] = meeting["VirtualParticipantId"]
    elif offer["LabelKey"]:
        item["LabelKey"] = offer["LabelKey"]
        item["LabelValue"] = offer["LabelValue"]
    if settings.get("keepAdvanceConsentDeviceDetails"):
        item["DeviceDetails"] = {
            "SourceIp": caller["source_ip"] or None,
            "UserAgent": caller["user_agent"] or None,
        }
    _table().put_item(Item=item, ConditionExpression="attribute_not_exists(PK)")
    # A new consent for the same thing replaces the one before it, so the
    # person has one current consent for each meeting and each organizer.
    for earlier in active:
        if advance.same_scope(earlier, item):
            _withdraw(email, str(earlier["ConsentId"]), now, "replaced")
    LOGGER.info("consent given in advance, scope %s", offer["Scope"])
    return consent_out(item, now)


def withdraw_advance_consent(caller: Mapping[str, Any], data: Mapping[str, Any]) -> Dict:
    email = account_email(caller)
    consent_id = data.get("ConsentId")
    if not isinstance(consent_id, str) or not re.fullmatch(r"[0-9]{14}-[0-9a-f]{12}", consent_id):
        raise BadRequest("ConsentId is not one of your consents")
    now = datetime.now(timezone.utc)
    updated = _withdraw(email, consent_id, now, "withdrawn")
    if updated is not None:
        pass_withdrawal_on(updated, now)
    if updated is None:
        item = (
            _table()
            .get_item(Key={"PK": _partition(email), "SK": f"ac#{consent_id}"}, ConsistentRead=True)
            .get("Item")
        )
        if not item:
            raise BadRequest("ConsentId is not one of your consents")
        updated = _plain(item)  # already withdrawn: answered as it is
    LOGGER.info("consent given in advance withdrawn")
    return consent_out(updated, now)


def pass_withdrawal_on(consent: Mapping[str, Any], now: datetime) -> int:
    """Tell each meeting that counted the consent that it was withdrawn.

    The meeting consent resolver records an objection from the attendee in
    each one still in progress (not ended), which stops capture there as an
    objection in the meeting does; a meeting that has ended is left as it is.
    The calls are asynchronous, so the withdrawal answers at once; the Virtual
    Participant reads the record within about 10 seconds. Answers how many
    meetings were told.
    """
    meetings = consent.get("AppliedCallIds") or []
    if not MEETING_CONSENT_FUNCTION or not meetings:
        return 0
    told = 0
    for call_id in sorted(meetings)[:MAX_WITHDRAWAL_MEETINGS]:
        try:
            LAMBDA.invoke(
                FunctionName=MEETING_CONSENT_FUNCTION,
                InvocationType="Event",
                Payload=json.dumps(
                    {
                        "advanceConsentWithdrawn": {
                            "CallId": call_id,
                            "ConsentId": consent.get("ConsentId"),
                            "WithdrawnAt": advance.iso(now),
                        }
                    }
                ).encode(),
            )
            told += 1
        except (ClientError, BotoCoreError):
            LOGGER.exception("could not pass a withdrawal on to a meeting that counted it")
    return told


def _markdown_text(value: str) -> str:
    return _MARKDOWN_SPECIAL.sub(r"\\\1", value)


def _reserve_ask(username: str, now: float) -> bool:
    day = time.strftime("%Y-%m-%d", time.gmtime(now))
    try:
        _table().update_item(
            Key={"PK": f"usr#{username}", "SK": f"ask-to-be-added#{day}"},
            UpdateExpression="ADD #n :one SET ExpiresAfter = :e",
            ConditionExpression="attribute_not_exists(#n) OR #n < :max",
            ExpressionAttributeNames={"#n": "Count"},
            ExpressionAttributeValues={":one": 1, ":max": ASKS_PER_DAY, ":e": int(now) + 2 * 86400},
        )
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return False
        raise
    return True


def _notify_owner(meeting: Mapping[str, Any], email: str, name: str) -> bool:
    """Show the request in LMA: a meeting notification, which reaches everyone who can
    see the meeting, or the Virtual Participant's row, which its page shows to its
    owner and Admins."""
    who = f"{name} <{email}>" if name else email
    if meeting["Kind"] == "call":
        # pylint: disable-next=import-outside-toplevel
        from lma_notifications import NotificationError, publish_meeting_notification

        try:
            publish_meeting_notification(
                meeting["CallId"],
                category="meeting",
                type="participant_list_request",
                title="Asked to be added to the participant list",
                body=(
                    f"**{_markdown_text(who)}** opened this meeting's consent link and is not on "
                    "its participant list. To let them give consent in advance, add them in the "
                    "meeting's **Participants** section. Nothing about their consent is recorded "
                    "until then."
                ),
                severity="suggestion",
                dedup_key=f"participant_list_request:{hashlib.sha256(email.encode()).hexdigest()[:16]}",
                actions=[
                    {"label": "Copy name and address", "kind": "copy", "payload": {"text": who}}
                ],
                source="advance-consent",
                # Well inside the function's own 30 seconds, so a request that
                # reached nobody is still forgotten and can be made again.
                timeout=10,
            )
        except (NotificationError, OSError, ValueError):
            LOGGER.exception("could not notify the meeting's owner")
            return False
        return True
    return _keep_request_on_vp(meeting, email, name)


def _keep_request_on_vp(meeting: Mapping[str, Any], email: str, name: str) -> bool:
    """Add the request to the Virtual Participant's row, which its page shows its owner.

    The row keeps the newest MAX_REQUESTS_PER_VP requests from people still not
    on its list: a request from someone added since, or an earlier one from the
    same address, is dropped as this one is added, so a full row never refuses
    a new request. Written on condition that the requests are as read, and read
    again on a conflict.
    """
    table = DYNAMODB.Table(VIRTUAL_PARTICIPANT_TABLE)
    for _attempt in range(3):
        vp = _vp_row(meeting["VirtualParticipantId"])
        if not vp:
            return False
        current = vp.get("participantListRequests")
        listed = {p["email"].casefold() for p in _participants(vp.get("participants"))}
        kept = [
            r
            for r in current or []
            if isinstance(r, Mapping)
            and isinstance(r.get("email"), str)
            and r["email"].casefold() not in listed
            and r["email"].casefold() != email
        ]
        kept.append(
            {
                "name": name or None,
                "email": email,
                "requestedAt": advance.iso(datetime.now(timezone.utc)),
            }
        )
        values: Dict[str, Any] = {":r": kept[-MAX_REQUESTS_PER_VP:]}
        if current is None:
            condition = "attribute_exists(id) AND attribute_not_exists(participantListRequests)"
        else:
            condition = "attribute_exists(id) AND participantListRequests = :old"
            values[":old"] = current
        try:
            table.update_item(
                Key={"id": meeting["VirtualParticipantId"]},
                UpdateExpression="SET participantListRequests = :r",
                ConditionExpression=condition,
                ExpressionAttributeValues=values,
            )
            return True
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise
    LOGGER.warning("the Virtual Participant's requests kept changing; this one was not kept")
    return False


def ask_to_be_added_to_meeting(caller: Mapping[str, Any], data: Mapping[str, Any]) -> Dict:
    email = account_email(caller)
    settings = consent_settings()
    if not settings.get("advanceConsentEnabled"):
        raise Refused("this deployment does not take consent given in advance")
    meeting = meeting_of(data)
    # A link to no meeting is answered as one to a meeting the caller is not
    # listed on, in this answer and the ones after it: the request is kept and
    # counted the same, and only nobody is told.
    if meeting is not None and listed_entry(meeting, email) is not None:
        return {"Requested": False, "Listed": True}
    key = _request_key(meeting, data)
    name = consent.clip(data.get("Name"), advance.MAX_NAME_LENGTH) or ""
    name = re.sub(r"[\r\n<>]", " ", name).strip()
    now = time.time()
    try:
        _table().put_item(
            Item={
                "PK": key,
                "SK": email,
                "Name": name or None,
                "RequestedAt": advance.iso(datetime.now(timezone.utc)),
                "ExpiresAfter": int(now) + REQUEST_SECONDS,
            },
            ConditionExpression="attribute_not_exists(PK) OR ExpiresAfter < :now",
            ExpressionAttributeValues={":now": int(now)},
        )
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return {"Requested": True, "Listed": False}
        raise
    if not _reserve_ask(caller["username"], now):
        _table().delete_item(Key={"PK": key, "SK": email})
        raise BadRequest(f"you can ask to be added at most {ASKS_PER_DAY} times a day")
    if meeting is not None and not _notify_owner(meeting, email, name):
        # Nobody was told, so the request is not remembered either: asking
        # again tries again.
        LOGGER.warning("a request to be added could not be shown; it can be made again")
        _table().delete_item(Key={"PK": key, "SK": email})
        raise BadRequest("your request could not be passed on; try again in a few minutes")
    LOGGER.info("asked to be added to a meeting's participant list")
    return {"Requested": True, "Listed": False}


OPERATIONS = {
    "listMyAdvanceConsents": list_my_advance_consents,
    "getAdvanceConsentInvitation": get_advance_consent_invitation,
    "giveAdvanceConsent": give_advance_consent,
    "withdrawAdvanceConsent": withdraw_advance_consent,
    "askToBeAddedToMeeting": ask_to_be_added_to_meeting,
}


def handler(event: Dict[str, Any], _context: Any) -> Dict[str, Any]:
    """AppSync Lambda resolver entry point (advanceConsent.request.vtl payload)."""
    field = (event.get("info") or {}).get("fieldName") or ""
    operation = OPERATIONS.get(field)
    if operation is None:
        raise BadRequest(f"unsupported field {field}")
    caller = caller_of(event)
    arguments = event.get("arguments") or {}
    data = arguments.get("input") if isinstance(arguments.get("input"), Mapping) else arguments
    try:
        return operation(caller, data or {})
    except Refused as error:
        LOGGER.info("refused %s: %s", field, error)
        raise
