#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""AppSync resolver for consent given in advance, by the signed-in person themselves.

Six operations, each admitted to every signed-in role (Admin, User and
Participant) and each acting on the caller's own records only:

=================================== ==============================================
``listMyAdvanceConsents``           The caller's consents given in advance, and
                                    the names their account goes by in meetings.
``getAdvanceConsentInvitation``     A meeting's consent page, as the caller sees
                                    it: whether they are on its participant list
                                    and, when they are, the meeting, the text they
                                    would agree to for the scope asked for, and
                                    their consents that cover it. Someone not on
                                    the list is told only that. With no meeting
                                    named: the text of a standing consent (for an
                                    organizer's meetings, or for every meeting on
                                    the deployment, until a date).
``giveAdvanceConsent``              A consent for the meeting, or for the meetings
                                    its organizer holds until a date. Only for a
                                    person on the meeting's list, and only for the
                                    text the page showed (by its hash). With no
                                    meeting named, a standing consent, for the
                                    text the page showed.
``withdrawAdvanceConsent``          Withdraws one of the caller's consents.
``askToBeAddedToMeeting``           For a signed-in person not on the list: shows,
                                    in LMA, the people who see the meeting's
                                    participant list that they asked to be added.
                                    Nothing about consent is recorded.
``updateMyAccountNames``            The caller's display name and short names, by
                                    which a meeting's attendees are matched to
                                    them on its participant list
                                    (lma_consent.account_names).
=================================== ==============================================

Who the caller is comes from the verified AppSync identity only: the username
and Cognito sub of the token, and the email address the user pool holds for
that username, which must be verified. No argument names a person. A
consent is stored under the caller's email address (``PK = pmc#<address>``,
compared ignoring case), with the Cognito sub of the account that gave it
(``Subject``), so reading or withdrawing one names only the caller's own
partition, and only the consents their own account gave: another person's
consent id is not found there, and a consent an earlier account with the same
address gave is not the caller's.

Three more operations are for Admins only (the Admin group, checked here as
well as in the schema), over everyone's consents given in advance
(lma_consent.administration):

=================================== ==============================================
``listAllAdvanceConsents``          Every consent, newest first, a page at a time,
                                    filtered by the start of the address, scope,
                                    state and end date.
``withdrawAdvanceConsentAsAdmin``   Withdraws one person's consent, as their own
                                    withdrawal does, recorded as withdrawn by the
                                    Admin, with an optional note.
``deleteAdvanceConsentRecords``     Removes one person's consents (some or all),
                                    withdrawing those in force first, and keeps an
                                    audit entry without their content.
=================================== ==============================================

A standing consent given with no meeting named names its organizer by the
address the person types. It is never looked up: the answer is the same
whether or not an account has that address, so the page tells nobody who uses
LMA. It names no meeting and gives no access to one; it counts in a meeting
only for an attendee matched to the person on that meeting's participant list.

The rules -- scopes, periods, text, what a consent covers -- are in
``lma_consent.advance`` in the configuration layer. See
docs/meeting-consent.md#consent-given-in-advance.
"""

from __future__ import annotations

import base64
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
from lma_consent import account_names, administration, advance  # configuration layer
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
#: The most meetings a withdrawal is passed on to, the most recently noted first.
MAX_WITHDRAWAL_MEETINGS = 50
#: The most earlier consents a consent names as replaced.
MAX_SUPERSEDED = 20
ADMIN_GROUP = os.environ.get("ADMIN_GROUP", "Admin")
#: The most consents one page of listAllAdvanceConsents answers, and the most
#: index pages it reads to fill one.
MAX_ADMIN_PAGE = 100
MAX_INDEX_READS = 10

DYNAMODB = boto3.resource("dynamodb", config=CLIENT_CONFIG)
DYNAMODB_CLIENT = boto3.client("dynamodb", config=CLIENT_CONFIG)
COGNITO = boto3.client("cognito-idp", config=CLIENT_CONFIG)
LAMBDA = boto3.client("lambda", config=CLIENT_CONFIG)

_STORE: Optional[ConfigurationStore] = None


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
    groups = claims.get("cognito:groups") or []
    if isinstance(groups, str):
        groups = [groups]
    return {
        "username": username,
        "sub": str(sub),
        "source_ip": str(source_ip or "")[:64],
        "user_agent": str(request.get("userAgent") or "")[:512],
        "is_admin": ADMIN_GROUP in groups,
    }


def account_attributes(caller: Mapping[str, Any]) -> Dict[str, Any]:
    """The caller's user pool attributes, with ``email`` their verified address in lower case.

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
    return {**attributes, "email": email.casefold()}


def account_email(caller: Mapping[str, Any]) -> str:
    """The caller's verified email address, as the user pool holds it, in lower case."""
    return account_attributes(caller)["email"]


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


def _meeting_row(call_id: str, consistent: bool = False) -> Optional[Dict[str, Any]]:
    item = (
        DYNAMODB.Table(EVENT_SOURCING_TABLE)
        .get_item(
            Key={"PK": f"c#{call_id}", "SK": f"c#{call_id}"},
            ProjectionExpression="CallId, #o, Participants, Labels, CreatedAt",
            ExpressionAttributeNames={"#o": "Owner"},
            ConsistentRead=consistent,
        )
        .get("Item")
    )
    return _plain(item) if item else None


def _vp_row(vp_id: str, consistent: bool = False) -> Optional[Dict[str, Any]]:
    item = (
        DYNAMODB.Table(VIRTUAL_PARTICIPANT_TABLE)
        .get_item(
            Key={"id": vp_id},
            ProjectionExpression=(
                "id, #o, meetingName, participants, labels, scheduledFor, CallId, "
                "participantListRequests"
            ),
            ExpressionAttributeNames={"#o": "Owner"},
            ConsistentRead=consistent,
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


def meeting_of(arguments: Mapping[str, Any], consistent: bool = False) -> Optional[Dict[str, Any]]:
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
    vp = _vp_row(vp_id, consistent)
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
    row = _meeting_row(linked, consistent) if isinstance(linked, str) and linked else None
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


def my_consents(email: str, subject: str, not_withdrawn: bool = False) -> List[Dict[str, Any]]:
    """The caller's consents, newest first: the newest MAX_CONSENTS_PER_PERSON,
    or with ``not_withdrawn`` every one not withdrawn, however old, read a page
    at a time (each person has at most MAX_ACTIVE_PER_PERSON in force).

    Only those their own account gave (``Subject``, its Cognito sub): a
    consent an earlier account with the same address gave is not theirs. The
    newest MAX_CONSENTS_PER_PERSON under the address are read before this
    filter, so an earlier account's consents count towards that number.
    """
    return [
        item
        for item in _consents_under(email, not_withdrawn)
        if subject and item.get("Subject") == subject
    ]


def _consents_under(email: str, not_withdrawn: bool) -> List[Dict[str, Any]]:
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
        "WithdrawnNote",
    )
    out = {name: item.get(name) for name in fields}
    out["Status"] = advance.status_of(item, now)
    out["DeviceDetailsKept"] = bool(item.get("DeviceDetails"))
    return out


def _new_consent_id(now: datetime) -> str:
    """Sortable by when it was given, so the newest are read first."""
    return f"{now.strftime('%Y%m%d%H%M%S')}-{secrets.token_hex(6)}"


def _withdraw(  # pylint: disable=too-many-arguments
    email: str,
    consent_id: str,
    now: datetime,
    reason: str,
    *,
    subject: str = "",
    by: str = "",
    note: str = "",
) -> Optional[Dict]:
    """Mark one consent withdrawn; None when it is not the caller's, already
    withdrawn, or expired.

    With ``subject``, only a consent that account gave. ``by`` and ``note``
    record an Admin's withdrawal on the person's behalf.
    """
    sets = "SET WithdrawnAt = :w, WithdrawnReason = :r, ExpiresAfter = :e"
    # Only one still in force: an expired consent stopped counting at
    # its end date, and keeps the expiry counted from then.
    condition = "attribute_exists(PK) AND attribute_not_exists(WithdrawnAt) AND ValidUntil > :w"
    values: Dict[str, Any] = {":w": advance.iso(now), ":r": reason, ":e": _expires_after(now)}
    if subject:
        condition += " AND Subject = :s"
        values[":s"] = subject
    if by:
        sets += ", WithdrawnBy = :b"
        values[":b"] = by[:256]
    if note:
        sets += ", WithdrawnNote = :n"
        values[":n"] = note
    try:
        response = _table().update_item(
            Key={"PK": _partition(email), "SK": f"ac#{consent_id}"},
            UpdateExpression=sets,
            ConditionExpression=condition,
            ExpressionAttributeValues=values,
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
    if scope not in advance.LINK_SCOPES:
        raise BadRequest(f"Scope must be one of {', '.join(advance.LINK_SCOPES)}")
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


def proposed_standing(arguments: Mapping[str, Any], settings: Mapping[str, Any], now) -> Dict:
    """The standing consent the arguments describe, given with no meeting named.

    ``organizer`` names the organizer by the address typed (``Organizer``) and
    may be limited to one label, typed as ``LabelKey`` and ``LabelValue``;
    ``deployment`` covers every meeting. Both run to a date (``ValidUntil``)
    within the Meeting Consent setting's longest period, and are given with the
    deployment's consent text (``advanceConsentText``), as from a meeting's link.
    """
    scope = arguments.get("Scope")
    if scope not in advance.STANDING_SCOPES:
        raise BadRequest(
            "With no meeting named, Scope must be one of " + ", ".join(advance.STANDING_SCOPES)
        )
    days = advance.max_days(settings.get("advanceConsentMaxDays"))
    try:
        until = advance.valid_until(scope, arguments.get("ValidUntil"), now, days)
        if scope == advance.SCOPE_ORGANIZER:
            organizer = advance.organizer_address(arguments.get("Organizer"))
            label = advance.typed_label(arguments.get("LabelKey"), arguments.get("LabelValue"))
        else:
            organizer, label = "", None
    except advance.InvalidConsent as error:
        raise BadRequest(str(error)) from error
    template = settings.get("advanceConsentText")
    text = advance.render_text(
        template,
        organizer=organizer or advance.EACH_ORGANIZER,
        meeting_name="",
        scope=advance.scope_phrase(scope, until=until, label=label),
        standing=True,
    )
    return {
        "Scope": scope,
        "Organizer": organizer or None,
        "ValidUntil": advance.iso(until),
        "LabelKey": label[0] if label else None,
        "LabelValue": label[1] if label else None,
        "NoticeText": text,
        "NoticeHash": advance.text_hash(text),
        "NoticeVersion": advance.text_version(template),
        "MaxDays": days,
    }


def names_a_meeting(arguments: Mapping[str, Any]) -> bool:
    """Whether the arguments name a meeting (CallId or VirtualParticipantId)."""
    return bool(arguments.get("CallId") or arguments.get("VirtualParticipantId"))


def _covering_meeting(meeting: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "CallId": meeting.get("CallId"),
        "VirtualParticipantId": meeting.get("VirtualParticipantId"),
        "Owner": meeting.get("Owner"),
        "Labels": meeting.get("Labels"),
    }


# ── the operations ──────────────────────────────────────────────────────────


def names_out(profile: Optional[Mapping[str, Any]], attributes: Mapping[str, Any]) -> Dict:
    """The ``AccountNames`` GraphQL shape: the names kept for this account, and its user pool name.

    Names kept for another account that held the address are not this
    account's, and are answered as none.
    """
    mine = profile if profile and profile.get("Subject") == attributes.get("sub") else {}
    return {
        "DisplayName": mine.get("DisplayName") or None,
        "ShortNames": list(mine.get("ShortNames") or []),
        "AccountName": attributes.get("name") or None,
        "UpdatedAt": mine.get("UpdatedAt") or None,
    }


def list_my_advance_consents(caller: Mapping[str, Any], _arguments: Mapping[str, Any]) -> Dict:
    attributes = account_attributes(caller)
    email = attributes["email"]
    settings = consent_settings()
    now = datetime.now(timezone.utc)
    profile = account_names.read_profile(DYNAMODB_CLIENT, EVENT_SOURCING_TABLE, email)
    return {
        "Consents": [consent_out(item, now) for item in my_consents(email, caller["sub"])],
        "Enabled": bool(settings.get("advanceConsentEnabled")),
        "MaxDays": advance.max_days(settings.get("advanceConsentMaxDays")),
        "Email": email,
        "Names": names_out(profile, attributes),
    }


def update_my_account_names(caller: Mapping[str, Any], data: Mapping[str, Any]) -> Dict:
    """Keep the caller's display name and short names, replacing the ones before.

    Each is optional; with neither, the names are removed and the user pool's
    ``name``, when set, is the display name again.
    """
    attributes = account_attributes(caller)
    try:
        profile = account_names.save_profile(
            DYNAMODB_CLIENT,
            EVENT_SOURCING_TABLE,
            email=attributes["email"],
            display_name=data.get("DisplayName"),
            short_names=data.get("ShortNames"),
            subject=caller["sub"],
            updated_by=caller["username"],
            now=advance.iso(datetime.now(timezone.utc)),
        )
    except (account_names.InvalidNames, account_names.NamesChanged) as error:
        raise BadRequest(str(error)) from error
    LOGGER.info("account names updated by their owner")
    return names_out(profile, attributes)


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
    if not names_a_meeting(arguments) and arguments.get("Scope") in advance.STANDING_SCOPES:
        return standing_invitation(email, caller["sub"], arguments, settings, enabled)
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
        for item in _with_created_meetings(my_consents(email, caller["sub"], not_withdrawn=True))
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


def standing_invitation(
    email: str,
    subject: str,
    arguments: Mapping[str, Any],
    settings: Mapping[str, Any],
    enabled: bool,
) -> Dict:
    """The My consents page with no meeting named: the standing consent the
    arguments describe and its exact text, and the caller's consents in force
    for the same thing. Nothing about any meeting or any other person."""
    now = datetime.now(timezone.utc)
    answer: Dict[str, Any] = {
        "Listed": False,
        "Requested": False,
        "Enabled": enabled,
        "Email": email,
        "ActiveConsents": [],
    }
    if not enabled:
        return answer
    offer = proposed_standing(arguments, settings, now)
    answer.update(
        {
            key: offer[key]
            for key in (
                "Scope",
                "Organizer",
                "ValidUntil",
                "LabelKey",
                "LabelValue",
                "NoticeText",
                "NoticeHash",
                "MaxDays",
            )
        }
    )
    answer["ActiveConsents"] = [
        consent_out(item, now)
        for item in my_consents(email, subject, not_withdrawn=True)
        if advance.status_of(item, now) == advance.STATUS_ACTIVE and advance.same_scope(item, offer)
    ]
    return answer


def give_advance_consent(caller: Mapping[str, Any], data: Mapping[str, Any]) -> Dict:
    email = account_email(caller)
    settings = consent_settings()
    if not settings.get("advanceConsentEnabled"):
        raise Refused("this deployment does not take consent given in advance")
    now = datetime.now(timezone.utc)
    if names_a_meeting(data):
        meeting = meeting_of(data)
        if not meeting or listed_entry(meeting, email) is None:
            raise Refused("you are not on this meeting's participant list")
        if not meeting["Owner"]:
            raise BadRequest("the meeting has no owner")
        offer = proposed(meeting, data, settings, now)
    else:
        # A standing consent, given with no meeting named: it names no meeting.
        offer = proposed_standing(data, settings, now)
        meeting = {
            "Owner": offer["Organizer"],
            "Name": None,
            "CallId": "",
            "VirtualParticipantId": "",
        }
    if data.get("NoticeHash") != offer["NoticeHash"]:
        # What the page showed is not what would be stored: the settings, the
        # meeting or the date changed since. Nothing is recorded.
        raise BadRequest("the consent text has changed since the page showed it; read it again")
    active = [
        item
        for item in _with_created_meetings(my_consents(email, caller["sub"], not_withdrawn=True))
        if advance.status_of(item, now) == advance.STATUS_ACTIVE
    ]
    if len(active) >= MAX_ACTIVE_PER_PERSON:
        raise BadRequest(f"you have {MAX_ACTIVE_PER_PERSON} consents in force; withdraw one first")
    until = advance.parse_time(offer["ValidUntil"]) or now
    consent_id = _new_consent_id(now)
    item: Dict[str, Any] = {
        "PK": _partition(email),
        "SK": f"ac#{consent_id}",
        # Lists it in the meetings table's TypeDateIndex, for Admins.
        "ItemType": administration.ITEM_TYPE,
        "ConsentId": consent_id,
        "Email": email,
        "Subject": caller["sub"],
        "Username": caller["username"],
        "Source": advance.SOURCE_PRE_MEETING,
        "Scope": offer["Scope"],
        "GrantedAt": advance.iso(now),
        "ValidUntil": offer["ValidUntil"],
        "NoticeText": offer["NoticeText"],
        "NoticeHash": offer["NoticeHash"],
        "NoticeVersion": offer["NoticeVersion"],
        "ExpiresAfter": _expires_after(until),
    }
    if meeting["Owner"]:
        item["Organizer"] = meeting["Owner"]
    if meeting["Name"]:
        item["MeetingName"] = meeting["Name"]
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
    # A new consent for the same thing replaces the one before it, so the
    # person has one current consent for each meeting, each organizer (and
    # label) and the whole deployment. It
    # takes over the meetings that counted the earlier one and names it, so
    # withdrawing the new one reaches them too (pass_withdrawal_on).
    replaced = [earlier for earlier in active if advance.same_scope(earlier, item)]
    supersedes: List[str] = []
    applied: Dict[str, str] = {}
    for earlier in replaced:
        supersedes += [str(earlier["ConsentId"])] + list(earlier.get("Supersedes") or [])
        applied.update(earlier.get("AppliedAt") or {})
    if supersedes:
        item["Supersedes"] = list(dict.fromkeys(supersedes))[:MAX_SUPERSEDED]
    if applied:
        item["AppliedAt"] = applied
    _table().put_item(Item=item, ConditionExpression="attribute_not_exists(PK)")
    for earlier in replaced:
        _withdraw(email, str(earlier["ConsentId"]), now, "replaced", subject=caller["sub"])
    LOGGER.info("consent given in advance, scope %s", offer["Scope"])
    return consent_out(item, now)


def withdraw_advance_consent(caller: Mapping[str, Any], data: Mapping[str, Any]) -> Dict:
    email = account_email(caller)
    consent_id = data.get("ConsentId")
    if not isinstance(consent_id, str) or not re.fullmatch(r"[0-9]{14}-[0-9a-f]{12}", consent_id):
        raise BadRequest("ConsentId is not one of your consents")
    now = datetime.now(timezone.utc)
    updated = _withdraw(email, consent_id, now, "withdrawn", subject=caller["sub"])
    if updated is not None:
        pass_withdrawal_on(updated, now)
    if updated is None:
        item = (
            _table()
            .get_item(Key={"PK": _partition(email), "SK": f"ac#{consent_id}"}, ConsistentRead=True)
            .get("Item")
        )
        if not item or item.get("Subject") != caller["sub"]:
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
    meetings were told (lma_consent.administration.pass_withdrawal_on).
    """
    return administration.pass_withdrawal_on(LAMBDA, MEETING_CONSENT_FUNCTION, consent, now)


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


def _publish_request(call_id: str, email: str, name: str) -> bool:
    """The request as the meeting's ``participant_list_request`` notification.

    One notification per address (advance.request_dedup_key): a later request,
    or the same one published again, updates it.
    """
    # pylint: disable-next=import-outside-toplevel
    from lma_notifications import NotificationError, publish_meeting_notification

    try:
        publish_meeting_notification(
            call_id,
            **advance.participant_list_request_notification(email, name),
            # Well inside the function's own 30 seconds, so a request that
            # reached nobody is still forgotten and can be made again.
            timeout=10,
        )
    except (NotificationError, OSError, ValueError):
        LOGGER.exception("could not notify the meeting's owner")
        return False
    return True


def _notify_owner(meeting: Mapping[str, Any], email: str, name: str) -> bool:
    """Show the request in LMA: a meeting notification, which reaches everyone who can
    see the meeting, or the Virtual Participant's row, which its page shows to its
    owner and Admins.

    A request kept on the row is published to the meeting when the Virtual
    Participant creates it (the call event processor's START, see
    call_event_processor/participant_list_requests.py). A request kept while
    that happens could be read by neither, so once it is kept the meeting is
    looked for again, and the request published to it if it now exists.

    Both sides read consistently: the call event processor reads the row (the
    getVirtualParticipant resolver) after the meeting is written, and this
    reads the row and then the meeting after the request is written. Whichever
    write comes second, the read after it sees the other, so a request kept
    while the meeting is being created is published by at least one of them;
    the shared key per address makes a second publish update the first.
    """
    if meeting["Kind"] == "call":
        return _publish_request(meeting["CallId"], email, name)
    if not _keep_request_on_vp(meeting, email, name):
        return False
    # Kept on the row either way, so a failure from here on is only logged.
    try:
        # Consistent reads of the row and of its meeting, so a meeting created
        # moments ago is found.
        current = meeting_of(
            {"VirtualParticipantId": meeting["VirtualParticipantId"]}, consistent=True
        )
    except (ClientError, BotoCoreError):
        LOGGER.exception("could not look for the Virtual Participant's meeting again")
        return True
    if current is not None and current["Kind"] == "call" and listed_entry(current, email) is None:
        _publish_request(current["CallId"], email, name)
    return True


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
        # Consistent, so the condition below is checked against the row as it is.
        vp = _vp_row(meeting["VirtualParticipantId"], consistent=True)
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


# ── for Admins: everyone's consents given in advance ────────────────────────

_CONSENT_ID = re.compile(r"[0-9]{14}-[0-9a-f]{12}")
_DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def _require_admin(caller: Mapping[str, Any]) -> None:
    if not caller.get("is_admin"):
        raise Refused("only an Admin may administer everyone's consents")


def _address(value: Any, name: str, required: bool) -> str:
    text = value.strip().casefold() if isinstance(value, str) else ""
    if not text:
        if required:
            raise BadRequest(f"{name} is required")
        return ""
    if len(text) > 254 or re.search(r"[\s\x00-\x1f\x7f]", text):
        raise BadRequest(f"{name} must be an email address or the start of one")
    return text


def _next_key(token: Any) -> Optional[Dict[str, Any]]:
    """The index key a NextToken names, or None for the first page."""
    if not token:
        return None
    try:
        key = json.loads(base64.urlsafe_b64decode(str(token).encode()).decode())
    except (ValueError, UnicodeDecodeError) as error:
        raise BadRequest("NextToken is not one this operation answered") from error
    ok = (
        isinstance(key, dict)
        and set(key) == {"ItemType", "SK", "PK"}
        and key["ItemType"] == {"S": administration.ITEM_TYPE}
        and all(isinstance(key[k], dict) and isinstance(key[k].get("S"), str) for k in key)
        and key["PK"]["S"].startswith("pmc#")
        and key["SK"]["S"].startswith("ac#")
    )
    if not ok:
        raise BadRequest("NextToken is not one this operation answered")
    return key


def _token(key: Optional[Mapping[str, Any]]) -> Optional[str]:
    return base64.urlsafe_b64encode(json.dumps(key).encode()).decode() if key else None


def _admin_filters(arguments: Mapping[str, Any]) -> Dict[str, Any]:
    scope = arguments.get("Scope") or None
    if scope is not None and scope not in advance.SCOPES:
        raise BadRequest(f"Scope must be one of {', '.join(advance.SCOPES)}")
    state = arguments.get("State") or None
    states = (advance.STATUS_ACTIVE, advance.STATUS_EXPIRED, advance.STATUS_WITHDRAWN)
    if state is not None and state not in states:
        raise BadRequest(f"State must be one of {', '.join(states)}")
    before = arguments.get("ExpiringBefore") or None
    if before is not None and not (isinstance(before, str) and _DAY.fullmatch(before)):
        raise BadRequest("ExpiringBefore must be a date, YYYY-MM-DD")
    return {
        "person": _address(arguments.get("Person"), "Person", required=False),
        "scope": scope,
        "state": state,
        "before": f"{before}T00:00:00Z" if before else None,
    }


def _admin_matches(item: Mapping[str, Any], filters: Mapping[str, Any], now: datetime) -> bool:
    if filters["scope"] and item.get("Scope") != filters["scope"]:
        return False
    if filters["state"] and advance.status_of(item, now) != filters["state"]:
        return False
    if filters["before"] and not str(item.get("ValidUntil") or "") < filters["before"]:
        return False
    return True


def admin_consent_out(
    item: Mapping[str, Any], now: datetime, profiles: Mapping[str, Mapping[str, Any]]
) -> Dict[str, Any]:
    """The ``AdminAdvanceConsent`` shape: the consent, who withdrew it, the
    meetings that counted it, and the display name of the account that gave it."""
    out = consent_out(item, now)
    profile = profiles.get(str(item.get("Email") or "")) or {}
    mine = profile.get("Subject") and profile.get("Subject") == item.get("Subject")
    applied = item.get("AppliedAt") or {}
    out.update(
        {
            "WithdrawnBy": item.get("WithdrawnBy"),
            "DisplayName": (profile.get("DisplayName") or None) if mine else None,
            "Supersedes": list(item.get("Supersedes") or []),
            "CountedIn": [
                {"CallId": call_id, "At": applied[call_id]}
                for call_id in sorted(applied, key=lambda c: str(applied[c]), reverse=True)
            ]
            if isinstance(applied, Mapping)
            else [],
        }
    )
    return out


def list_all_advance_consents(caller: Mapping[str, Any], arguments: Mapping[str, Any]) -> Dict:
    """Every consent given in advance, newest first, filtered, a page at a time."""
    _require_admin(caller)
    filters = _admin_filters(arguments)
    try:
        limit = max(1, min(int(arguments.get("Limit") or 50), MAX_ADMIN_PAGE))
    except (TypeError, ValueError) as error:
        raise BadRequest("Limit must be a number") from error
    key = _next_key(arguments.get("NextToken"))
    now = datetime.now(timezone.utc)
    found: List[Dict[str, Any]] = []
    next_key: Optional[Dict[str, Any]] = None
    for _read in range(MAX_INDEX_READS):
        query: Dict[str, Any] = {
            "TableName": EVENT_SOURCING_TABLE,
            "IndexName": administration.INDEX_NAME,
            "KeyConditionExpression": "ItemType = :t",
            "ExpressionAttributeValues": {":t": {"S": administration.ITEM_TYPE}},
            "ScanIndexForward": False,
            "Limit": MAX_ADMIN_PAGE,
        }
        if filters["person"]:
            query["FilterExpression"] = "begins_with(PK, :p)"
            query["ExpressionAttributeValues"][":p"] = {"S": f"pmc#{filters['person']}"}
        if key:
            query["ExclusiveStartKey"] = key
        response = DYNAMODB_CLIENT.query(**query)
        entries = [
            administration.item_of(raw)
            for raw in response.get("Items") or []
            if raw.get("SK", {}).get("S", "").startswith("ac#")
        ]
        try:
            items = administration.read_items(
                DYNAMODB_CLIENT,
                EVENT_SOURCING_TABLE,
                [{"PK": {"S": e["PK"]}, "SK": {"S": e["SK"]}} for e in entries],
            )
        except administration.ConsentsUnreadable as error:
            raise BadRequest(str(error)) from error
        key = response.get("LastEvaluatedKey")
        more_here = False
        for position, entry in enumerate(entries):
            item = items.get((entry["PK"], entry["SK"]))
            if item and _admin_matches(item, filters, now):
                found.append(item)
                if len(found) == limit:
                    more_here = position + 1 < len(entries)
                    break
        if len(found) == limit:
            # The next page starts after the last consent answered, when
            # anything is left to read.
            next_key = administration.index_key(found[-1]) if (more_here or key) else None
            break
        # The index read on from here, or nothing more to read.
        next_key = key
        if not key:
            break
    try:
        profiles = account_names.read_profiles(
            DYNAMODB_CLIENT, EVENT_SOURCING_TABLE, {str(i.get("Email") or "") for i in found}
        )
    except account_names.NamesChanged as error:
        # The names are read for the page as a whole; another read usually succeeds.
        raise BadRequest(
            "The names of the people on this page could not be read; try again"
        ) from error
    LOGGER.info("an Admin listed %d consents given in advance", len(found))
    return {
        "Consents": [admin_consent_out(item, now, profiles) for item in found],
        "NextToken": _token(next_key),
    }


def _consent_ref(data: Mapping[str, Any]) -> tuple:
    email = _address(data.get("Email"), "Email", required=True)
    consent_id = data.get("ConsentId")
    if not isinstance(consent_id, str) or not _CONSENT_ID.fullmatch(consent_id):
        raise BadRequest("ConsentId is not a consent given in advance")
    return email, consent_id


def withdraw_advance_consent_as_admin(caller: Mapping[str, Any], data: Mapping[str, Any]) -> Dict:
    """Withdraw one person's consent on their behalf, with the same effect as their own withdrawal."""
    _require_admin(caller)
    email, consent_id = _consent_ref(data)
    note = consent.clip(data.get("Note"), administration.MAX_NOTE_LENGTH) or ""
    now = datetime.now(timezone.utc)
    updated = _withdraw(
        email, consent_id, now, administration.REASON_ADMIN, by=caller["username"], note=note
    )
    if updated is not None:
        pass_withdrawal_on(updated, now)
        LOGGER.info("an Admin withdrew a consent given in advance")
    else:
        item = (
            _table()
            .get_item(Key={"PK": _partition(email), "SK": f"ac#{consent_id}"}, ConsistentRead=True)
            .get("Item")
        )
        if not item:
            raise BadRequest("There is no such consent")
        updated = _plain(item)  # no longer in force: answered as it is
    return admin_consent_out(updated, now, {})


def delete_advance_consent_records(caller: Mapping[str, Any], data: Mapping[str, Any]) -> Dict:
    """Permanently remove one person's consents, withdrawing those in force first."""
    _require_admin(caller)
    email = _address(data.get("Email"), "Email", required=True)
    ids = data.get("ConsentIds")
    every = bool(data.get("AllForPerson"))
    if every == bool(ids):
        raise BadRequest("Name the consents with ConsentIds, or set AllForPerson, not both")
    if ids and (
        not isinstance(ids, list)
        or len(ids) > administration.MAX_ERASED
        or not all(isinstance(i, str) and _CONSENT_ID.fullmatch(i) for i in ids)
    ):
        raise BadRequest("ConsentIds must be consents given in advance")
    now = datetime.now(timezone.utc)
    try:
        answer = administration.erase(
            DYNAMODB_CLIENT,
            EVENT_SOURCING_TABLE,
            email=email,
            erased_by=caller["username"],
            now=now,
            expires_after=_expires_after(now),
            consent_ids=None if every else ids,
            pass_on=lambda written: pass_withdrawal_on(written, now),
        )
    except administration.ConsentsUnreadable as error:
        # Raised before anything is withdrawn or removed.
        raise BadRequest(str(error)) from error
    LOGGER.info(
        "an Admin erased %d consents given in advance (%d withdrawn first, more remain: %s)",
        answer["Deleted"],
        answer["Withdrawn"],
        answer["MoreRemain"],
    )
    return {k: answer[k] for k in ("Deleted", "Withdrawn", "ConsentIds", "AuditId", "MoreRemain")}


OPERATIONS = {
    "listMyAdvanceConsents": list_my_advance_consents,
    "getAdvanceConsentInvitation": get_advance_consent_invitation,
    "giveAdvanceConsent": give_advance_consent,
    "withdrawAdvanceConsent": withdraw_advance_consent,
    "askToBeAddedToMeeting": ask_to_be_added_to_meeting,
    "updateMyAccountNames": update_my_account_names,
    "listAllAdvanceConsents": list_all_advance_consents,
    "withdrawAdvanceConsentAsAdmin": withdraw_advance_consent_as_admin,
    "deleteAdvanceConsentRecords": delete_advance_consent_records,
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
