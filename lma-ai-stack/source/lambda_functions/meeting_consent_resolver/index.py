#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""AppSync resolver for a meeting's consent record.

One function for the query and every mutation, because each of them reads the
meeting row, applies the same access rule and writes one attribute of it,
``Consent``. What the record means is decided in ``lma_consent`` (the
configuration layer), so this function and the capture clients decide alike. See docs/meeting-consent.md.

Who may do what:

=============================== ==================================================
``getMeetingConsent``           The ``getCall`` rule: an Admin, the meeting's owner,
                                somebody it is shared with, or a backend role.
``startMeetingConsent``         The owner, an Admin or a backend role.
``recordMeetingConsentNotice``  The owner, an Admin or a backend role.
``setMeetingConsentParticipants`` The owner, an Admin or a backend role.
``recordMeetingConsentEvent``   A backend role, for a reply read from the chat or
                                the speech. A signed-in user with access to the
                                meeting may record an objection; only the owner or
                                an Admin may record consent on a participant's
                                behalf. A signed-in user's entry is recorded as
                                source ``ui`` under their own username.
``attestMeetingConsent``        The meeting's owner, or a backend role relaying
                                the owner's attestation, recorded as the owner's.
``addMeetingConsentEvidence``   A backend role, for an image it stored under the
                                meeting's own evidence folder.
``verifyMeetingConsentEvidence`` The ``getCall`` rule. Runs the evidence verifier
                                Lambda on one image and stores its result.
=============================== ==================================================

Every write is a read of the meeting row followed by an update conditional on
the version of the consent record that was read, retried a few times, so two
replies arriving together are both kept.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Dict, List, Literal, Mapping, Optional

import boto3
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.config import Config as BotoCoreConfig
from botocore.exceptions import BotoCoreError, ClientError

# pylint: disable=import-error
from lma_config import (  # configuration layer
    MEETING_CONSENT,
    ConfigurationError,
    ConfigurationStore,
    ConfigurationTableNotConfigured,
)
from lma_consent import rules as consent  # configuration layer

# pylint: enable=import-error

# Stdlib logging rather than Powertools: this function carries only the
# configuration layer, which has no third-party dependencies.
LOGGER = logging.getLogger()
LOGGER.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

CLIENT_CONFIG = BotoCoreConfig(retries={"mode": "adaptive", "max_attempts": 3})
REGION = os.environ.get("AWS_REGION", "us-east-1")
EVENT_SOURCING_TABLE = os.environ.get("EVENT_SOURCING_TABLE", "")
RECORDINGS_BUCKET = os.environ.get("RECORDINGS_BUCKET", "")
EVIDENCE_PREFIX = os.environ.get("CONSENT_EVIDENCE_PREFIX", "lma-consent-evidence/")
ADMIN_GROUP = os.environ.get("ADMIN_GROUP", "Admin")
LINK_SECONDS = int(os.environ.get("LINK_SECONDS", "3600"))
VERIFIER_FUNCTION = os.environ.get("VERIFIER_FUNCTION_NAME", "")

#: Who a backend role's writes are attributed to when it names nobody.
BACKEND_ATTRIBUTION = "LMA"
#: Attempts at the read-and-conditional-write before a conflict is reported.
WRITE_ATTEMPTS = 5
EVIDENCE_KINDS = ("screenshot", "invitation")
MAX_KEY_LENGTH = 256

DYNAMODB = boto3.client("dynamodb", config=CLIENT_CONFIG)
# The verifier makes one model call; wait for it a little longer than AppSync
# waits for this function, so a slow check still stores its result.
LAMBDA = boto3.client(
    "lambda",
    config=BotoCoreConfig(read_timeout=70, retries={"mode": "standard", "max_attempts": 1}),
)
SERIALIZER = TypeSerializer()
DESERIALIZER = TypeDeserializer()


def _s3_client(addressing_style: Literal["virtual", "path"]) -> Any:
    """SigV4 links on the deployment's regional endpoint, in its partition."""
    return boto3.client(
        "s3",
        region_name=REGION,
        config=BotoCoreConfig(
            signature_version="s3v4",
            s3={"addressing_style": addressing_style, "us_east_1_regional_endpoint": "regional"},
            retries={"mode": "adaptive", "max_attempts": 3},
        ),
    )


# Virtual-hosted links, except for a bucket whose name has dots: that name does
# not match S3's TLS certificate as a host name, so its links are path-style.
S3 = _s3_client("path" if "." in RECORDINGS_BUCKET else "virtual")

_STORE: Optional[ConfigurationStore] = None


class ForbiddenError(Exception):
    """The caller may not do this to the meeting."""


class BadRequestError(Exception):
    """An argument that cannot be used."""


class ConflictError(Exception):
    """The record kept changing under the write; the caller may retry."""


class ConfigurationUnavailableError(Exception):
    """The deployment's consent settings could not be read."""


class VerificationFailedError(Exception):
    """The evidence could not be checked; the message says why."""


def now_iso() -> str:
    """The current time as an AWSDateTime, in UTC with milliseconds."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def posixify_filename(filename: str) -> str:
    """The folder name producers use for a meeting; the same rule as meeting deletion."""
    name = re.sub(r"[^a-zA-Z0-9_.]", "_", filename)
    name = re.sub(r"^_+", "", name)
    return re.sub(r"_+$", "", name)


def evidence_folder(call_id: str) -> str:
    return f"{EVIDENCE_PREFIX}{posixify_filename(call_id)}/"


# ── settings ────────────────────────────────────────────────────────────────


def consent_settings() -> Dict[str, Any]:
    """The deployment's consent settings, read now (the section's tier is immediate).

    Raises ConfigurationUnavailableError rather than falling back to "off": a
    deployment that requires consent must not start meetings without it because
    its settings could not be read.
    """
    global _STORE  # pylint: disable=global-statement
    try:
        if _STORE is None:
            _STORE = ConfigurationStore()
        values = _STORE.get_section(MEETING_CONSENT.name).values
    except ConfigurationTableNotConfigured:
        # A function deployed without the table name: the shipped defaults, in
        # which consent is off. Every deployed copy is given the table.
        LOGGER.warning("CONFIGURATION_TABLE_NAME is unset; using the shipped consent defaults")
        values = {}
    except (ClientError, BotoCoreError, ConfigurationError) as error:
        LOGGER.exception("could not read the meeting consent settings")
        raise ConfigurationUnavailableError(
            "the meeting consent settings could not be read; try again"
        ) from error
    return consent.effective_settings(values)


def settings_out(settings: Mapping[str, Any]) -> Dict[str, Any]:
    """The ``MeetingConsentSettings`` GraphQL shape."""
    return {
        "Mode": settings["mode"],
        "Rule": settings["consentRule"],
        "NotifyNoticeText": settings["notifyNoticeText"],
        "OptInNoticeText": settings["optInNoticeText"],
        "ConsentPhrases": list(settings["consentPhrases"]),
        "ObjectionPhrases": list(settings["objectionPhrases"]),
        "AllowPerMeetingOverride": bool(settings["allowPerMeetingOverride"]),
        "KeepEvidenceScreenshots": bool(settings["keepEvidenceScreenshots"]),
        "VerifierModelId": settings["verifierModelId"],
    }


# ── the caller and the meeting ──────────────────────────────────────────────


def caller_of(event: Mapping[str, Any]) -> Dict[str, Any]:
    """Who is asking, from the AppSync ``identity`` block only.

    The rule ``getCall``'s resolver applies, as video_keyframes_resolver reads
    it: a request signed with IAM by one of LMA's own roles carries an
    assumed-role ``userArn`` and no user-pool claims, and is the backend.
    """
    identity = event.get("identity") or {}
    claims = identity.get("claims") or {}
    groups = claims.get("cognito:groups") or identity.get("groups") or []
    if isinstance(groups, str):
        groups = [g.strip() for g in groups.split(",") if g.strip()]
    user_arn = str(identity.get("userArn") or "")
    is_backend = not claims and ":assumed-role/" in user_arn
    return {
        # An IAM identity's username is its role id and session name: not a
        # person, so never matched against the meeting's Owner or SharedWith and
        # never written as who recorded something.
        "username": "" if is_backend else str(identity.get("username") or ""),
        "subject": claims.get("sub") or identity.get("sub") or "",
        "is_admin": ADMIN_GROUP in groups,
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


def _meeting_key(call_id: str) -> Dict[str, Any]:
    return {"PK": {"S": f"c#{call_id}"}, "SK": {"S": f"c#{call_id}"}}


def read_meeting(call_id: str) -> Optional[Dict[str, Any]]:
    """The meeting row's owner, sharing and consent record, read consistently."""
    item = DYNAMODB.get_item(
        TableName=EVENT_SOURCING_TABLE,
        Key=_meeting_key(call_id),
        ConsistentRead=True,
        ProjectionExpression="CallId, #o, SharedWith, #c",
        ExpressionAttributeNames={"#o": "Owner", "#c": "Consent"},
    ).get("Item")
    if not item:
        return None
    return {key: _plain(DESERIALIZER.deserialize(value)) for key, value in item.items()}


def access_of(caller: Mapping[str, Any], meeting: Mapping[str, Any]) -> Dict[str, bool]:
    """What the caller is to this meeting."""
    username = caller["username"]
    is_owner = bool(username) and meeting.get("Owner") == username
    is_shared = bool(username) and username in shared_with_entries(meeting.get("SharedWith"))
    return {
        "admin": bool(caller["is_admin"]),
        "backend": bool(caller["is_backend"]),
        "owner": is_owner,
        "shared": is_shared,
    }


def refuse(caller: Mapping[str, Any], call_id: str, action: str) -> ForbiddenError:
    LOGGER.info("refused %s on %s to %s", action, call_id, caller["subject"] or "(none)")
    return ForbiddenError("Unauthorized: you may not do this to this meeting's consent")


def load(caller: Mapping[str, Any], call_id: str) -> tuple:
    """The meeting and the caller's access to it, refusing a caller with none.

    A meeting that does not exist gets the same answer as one the caller may
    not see, as in getCall; a backend role is told that it does not exist.
    """
    meeting = read_meeting(call_id)
    if meeting is None:
        if caller["is_backend"]:
            raise BadRequestError(f"no meeting {call_id}")
        raise refuse(caller, call_id, "read")
    access = access_of(caller, meeting)
    if not any(access.values()):
        raise refuse(caller, call_id, "read")
    return meeting, access


def actor(caller: Mapping[str, Any]) -> str:
    return caller["username"] or BACKEND_ATTRIBUTION


# ── output ──────────────────────────────────────────────────────────────────


def sign(key: Any, call_id: str) -> Optional[str]:
    """A link to an evidence image, only for one in the meeting's own folder."""
    if not isinstance(key, str) or not RECORDINGS_BUCKET:
        return None
    if not key.startswith(evidence_folder(call_id)) or ".." in key:
        return None
    return S3.generate_presigned_url(
        "get_object",
        Params={"Bucket": RECORDINGS_BUCKET, "Key": key},
        ExpiresIn=LINK_SECONDS,
    )


def consent_out(record: Optional[Mapping[str, Any]], call_id: str, signed: bool) -> Any:
    """The ``MeetingConsent`` GraphQL shape, with evidence links when ``signed``."""
    if not record:
        return None
    shaped = copy.deepcopy(dict(record))
    for item in shaped.get("Evidence") or []:
        kept = signed and not item.get("ImageDeletedAt")
        item["SignedUrl"] = sign(item.get("S3Key"), call_id) if kept else None
    return shaped


def update_out(item: Mapping[str, Any], call_id: str) -> Dict[str, Any]:
    """The ``MeetingConsentUpdate`` a mutation answers with.

    SharedWith is passed through as stored: dDbPutCondition.response.vtl, this
    resolver's response template, rewrites it as "[a, b]" for the
    subscription filter.
    """
    return {
        "CallId": call_id,
        "Owner": item.get("Owner"),
        "SharedWith": item.get("SharedWith"),
        "Consent": consent_out(item.get("Consent"), call_id, signed=False),
    }


# ── writing ─────────────────────────────────────────────────────────────────


def write_consent(
    caller: Mapping[str, Any],
    call_id: str,
    change: Callable[[Optional[Dict[str, Any]], Dict[str, Any], Dict[str, bool], str], Dict],
) -> Dict[str, Any]:
    """Read the meeting, apply ``change`` to its consent record, write it back.

    ``change(record, meeting, access, now)`` returns the new record (or raises).
    The write is conditional on the record's version as read, and the read,
    access check and change are repeated on a conflict, so concurrent writers
    never overwrite each other's entries.
    """
    for _attempt in range(WRITE_ATTEMPTS):
        meeting, access = load(caller, call_id)
        current = meeting.get("Consent") or None
        now = now_iso()
        updated = change(copy.deepcopy(current) if current else None, meeting, access, now)
        consent.apply_evaluation(updated, now)
        version = int((current or {}).get("Version") or 0)
        updated["Version"] = version + 1
        updated["UpdatedAt"] = now
        try:
            consent.fit_to_budget(updated)
        except consent.RecordFull as error:
            raise BadRequestError(str(error)) from error
        names = {"#c": "Consent"}
        values = {":c": SERIALIZER.serialize(_dynamo(updated))}
        if current:
            condition = "attribute_exists(PK) AND #c.#v = :v"
            names["#v"] = "Version"
            values[":v"] = {"N": str(version)}
        else:
            condition = "attribute_exists(PK) AND attribute_not_exists(#c)"
        try:
            DYNAMODB.update_item(
                TableName=EVENT_SOURCING_TABLE,
                Key=_meeting_key(call_id),
                UpdateExpression="SET #c = :c",
                ConditionExpression=condition,
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
            )
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                LOGGER.info("consent of %s changed while writing; reading it again", call_id)
                continue
            raise
        LOGGER.info(
            "consent of %s: version %d, status %s, capture allowed %s",
            call_id,
            updated["Version"],
            updated["Status"],
            updated["CaptureAllowed"],
        )
        # Owner and SharedWith as read: the write was conditional on the version
        # read with them, so they are the row's as of this write.
        return update_out({**meeting, "Consent": updated}, call_id)
    raise ConflictError("the meeting's consent kept changing; try again")


def require_started(record: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not record:
        raise BadRequestError("consent has not been started for this meeting")
    return record


def required_text(value: Any, name: str, limit: int) -> str:
    text = consent.clip(value, limit)
    if not text:
        raise BadRequestError(f"{name} is required")
    return text


def one_of(value: Any, allowed: tuple, name: str, default: Optional[str] = None) -> str:
    if value is None and default is not None:
        return default
    if value not in allowed:
        raise BadRequestError(f"{name} must be one of {', '.join(allowed)}")
    return value


# ── the operations ──────────────────────────────────────────────────────────


def get_meeting_consent(caller: Mapping[str, Any], arguments: Mapping[str, Any]) -> Dict:
    call_id = str(arguments["CallId"])
    meeting, _access = load(caller, call_id)
    record = meeting.get("Consent") or None
    try:
        settings: Optional[Dict[str, Any]] = consent_settings()
    except ConfigurationUnavailableError:
        # A started record decides its own gate; only a meeting without one
        # needs the deployment's mode to be judged.
        if not record:
            raise
        settings = None
    return {
        "CallId": call_id,
        "Consent": consent_out(record, call_id, signed=True),
        "CaptureAllowed": consent.capture_allowed(
            record, settings["mode"] if settings else consent.MODE_OFF
        ),
        "Settings": settings_out(settings) if settings else None,
    }


def requested_choice(
    data: Mapping[str, Any], current: Optional[Mapping[str, Any]], settings: Mapping[str, Any]
) -> tuple:
    """The mode and rule a start asks for.

    An omitted Mode or Rule means the meeting's own, once its consent has been
    started, and the deployment's before that. So a capture client that
    restarts and names nothing keeps the choice the meeting started with, even
    when the deployment's settings have changed since.
    """
    started = current if current and current.get("Mode") in consent.MODES else None
    mode = one_of(
        data.get("Mode"),
        consent.MODES,
        "Mode",
        default=started["Mode"] if started else settings["mode"],
    )
    if mode != consent.MODE_OPT_IN:
        return mode, None
    if started and started["Mode"] == mode and started.get("Rule") in consent.RULES:
        default_rule = started["Rule"]
    else:
        default_rule = settings["consentRule"]
    return mode, one_of(data.get("Rule"), consent.RULES, "Rule", default=default_rule)


def start_meeting_consent(caller: Mapping[str, Any], data: Mapping[str, Any]) -> Dict:
    call_id = data["CallId"]
    meeting, access = load(caller, call_id)
    if not (access["owner"] or access["admin"] or access["backend"]):
        raise refuse(caller, call_id, "start")
    current = meeting.get("Consent") or None
    try:
        settings = consent_settings()
    except ConfigurationUnavailableError:
        # A restart that names no other choice needs no settings: it keeps the
        # meeting's own. Judged as requested_choice judges it, so whether the
        # settings could be read never changes what counts as a restart.
        started = current if current and current.get("Mode") in consent.MODES else None
        if started:
            mode = one_of(data.get("Mode"), consent.MODES, "Mode", default=started["Mode"])
            if mode == consent.MODE_OPT_IN and data.get("Rule") is not None:
                one_of(data.get("Rule"), consent.RULES, "Rule")
            rule = (
                (data.get("Rule") or started.get("Rule")) if mode == consent.MODE_OPT_IN else None
            )
            if mode == started["Mode"] and rule == started.get("Rule"):
                return update_out(meeting, call_id)
        raise
    mode, rule = requested_choice(data, current, settings)
    if current and current.get("Mode") == mode and current.get("Rule") == rule:
        # A capture client that restarts starts again: answer with the record
        # as it is, without a write. Its notice is the one it started with;
        # NoticeText, LmaUser and MeetingName are not read again. Subscribers
        # still receive the unchanged record, as for any successful mutation.
        return update_out(meeting, call_id)
    permitted, why = consent.override_permitted(settings, mode, rule)
    if not permitted:
        raise BadRequestError(why)
    custom_notice = consent.clip(data.get("NoticeText"), consent.MAX_NOTICE_LENGTH)
    if custom_notice and not settings["allowPerMeetingOverride"]:
        raise BadRequestError("this deployment does not allow a meeting to use its own notice")

    def change(record, _meeting, access, now):
        if not (access["owner"] or access["admin"] or access["backend"]):
            raise refuse(caller, call_id, "start")
        if record:
            if record.get("Mode") == mode and record.get("Rule") == rule:
                return record
            under_way = (
                record.get("Events")
                or record.get("Notices")
                or record.get("Attestation")
                or record.get("Evidence")
            )
            if under_way:
                raise BadRequestError(
                    f"consent for this meeting is already under way in mode {record.get('Mode')}"
                )
        notice = None
        if mode != consent.MODE_OFF:
            template = custom_notice or (
                settings["optInNoticeText"]
                if mode == consent.MODE_OPT_IN
                else settings["notifyNoticeText"]
            )
            notice = consent.render_notice(
                template,
                lma_user=consent.clip(data.get("LmaUser"), consent.MAX_NAME_LENGTH) or "",
                meeting=consent.clip(data.get("MeetingName"), consent.MAX_NAME_LENGTH) or "",
                consent_phrase=(settings["consentPhrases"] or [""])[0],
                objection_phrase=(settings["objectionPhrases"] or [""])[0],
            )
        started = {
            "Mode": mode,
            "Rule": rule,
            "NoticeText": notice,
            "NoticePostedAt": None,
            "Notices": [],
            "Participants": [],
            "ParticipantsSeen": [],
            "Events": [],
            "Attestation": None,
            "Evidence": [],
            "StatusHistory": [],
            "StartedAt": now,
            "StartedBy": actor(caller),
        }
        if record:
            # A change of mode before anything was posted or replied: who is
            # present, when consent was first started and the history carry
            # over, and the change itself is added to the history.
            for kept in ("Participants", "ParticipantsSeen", "StartedAt", "StartedBy"):
                started[kept] = record.get(kept) or started[kept]
            status, allowed, _reason = consent.evaluate(started)
            history = list(record.get("StatusHistory") or [])
            history.append(
                {
                    "Status": status,
                    "CaptureAllowed": allowed,
                    "At": now,
                    "Reason": f"the mode was changed to {mode}"
                    + (f" ({rule})" if rule else "")
                    + f" by {actor(caller)}",
                }
            )
            started["StatusHistory"] = history[-consent.MAX_STATUS_HISTORY :]
            started["Status"], started["CaptureAllowed"] = status, allowed
        return started

    return write_consent(caller, call_id, change)


def record_meeting_consent_notice(caller: Mapping[str, Any], data: Mapping[str, Any]) -> Dict:
    call_id = data["CallId"]
    channel = one_of(data.get("Channel"), consent.NOTICE_CHANNELS, "Channel")

    def change(record, _meeting, access, now):
        if not (access["owner"] or access["admin"] or access["backend"]):
            raise refuse(caller, call_id, "record a notice")
        record = require_started(record)
        posted_at = data.get("PostedAt") or now
        notice = {
            "Channel": channel,
            "Text": consent.clip(data.get("Text"), consent.MAX_NOTICE_LENGTH)
            or record.get("NoticeText"),
            "PostedAt": posted_at,
            "PostedBy": actor(caller),
        }
        notices = list(record.get("Notices") or []) + [notice]
        # The first notice is kept whatever follows: it is the one the
        # meeting's capture was started on.
        if len(notices) > consent.MAX_NOTICES:
            notices = notices[:1] + notices[-(consent.MAX_NOTICES - 1) :]
        record["Notices"] = notices
        record["NoticePostedAt"] = record.get("NoticePostedAt") or posted_at
        return record

    return write_consent(caller, call_id, change)


def set_meeting_consent_participants(caller: Mapping[str, Any], data: Mapping[str, Any]) -> Dict:
    call_id = data["CallId"]
    names = data.get("Participants")
    if not isinstance(names, list):
        raise BadRequestError("Participants must be a list of display names")
    present: List[str] = []
    keys = set()
    for name in names:
        clipped = consent.clip(name, consent.MAX_NAME_LENGTH)
        key = consent.normalize_name(clipped)
        if clipped and key not in keys:
            keys.add(key)
            present.append(clipped)
    if len(present) > consent.MAX_PARTICIPANTS:
        raise BadRequestError(f"at most {consent.MAX_PARTICIPANTS} participants can be listed")

    def change(record, _meeting, access, _now):
        if not (access["owner"] or access["admin"] or access["backend"]):
            raise refuse(caller, call_id, "list participants")
        record = require_started(record)
        record["Participants"] = present
        seen = list(record.get("ParticipantsSeen") or [])
        seen_keys = {consent.normalize_name(name) for name in seen}
        seen += [name for name in present if consent.normalize_name(name) not in seen_keys]
        record["ParticipantsSeen"] = seen[: consent.MAX_PARTICIPANTS_SEEN]
        return record

    return write_consent(caller, call_id, change)


def record_meeting_consent_event(caller: Mapping[str, Any], data: Mapping[str, Any]) -> Dict:
    call_id = data["CallId"]
    name = required_text(data.get("ParticipantName"), "ParticipantName", consent.MAX_NAME_LENGTH)
    decision = one_of(data.get("Decision"), consent.DECISIONS, "Decision")
    if caller["is_backend"]:
        source = one_of(data.get("Source"), ("chat", "voice"), "Source")
    else:
        # A signed-in user's entry is always their own, made on the page.
        source = "ui"

    def change(record, _meeting, access, now):
        record = require_started(record)
        if not access["backend"] and decision == consent.DECISION_CONSENT:
            if not (access["owner"] or access["admin"]):
                raise refuse(caller, call_id, "record consent")
        event = {
            "EventId": uuid.uuid4().hex[:16],
            "ParticipantName": name,
            "Decision": decision,
            "Source": source,
            "Text": consent.clip(data.get("Text"), consent.MAX_TEXT_LENGTH),
            "OccurredAt": data.get("OccurredAt") or now,
            "RecordedAt": now,
            "RecordedBy": actor(caller),
        }
        record["Events"] = consent.compact_events(list(record.get("Events") or []) + [event])
        return record

    return write_consent(caller, call_id, change)


def attest_meeting_consent(caller: Mapping[str, Any], data: Mapping[str, Any]) -> Dict:
    call_id = data["CallId"]
    method = one_of(data.get("Method"), consent.ATTESTATION_METHODS, "Method")
    statement = consent.clip(data.get("Statement"), consent.MAX_TEXT_LENGTH)
    named = consent.clip(data.get("AttestedBy"), consent.MAX_NAME_LENGTH)

    def change(record, meeting, access, now):
        # The owner's own statement: not an Admin's or a shared user's on their
        # behalf. A backend role relays one it received from the owner, and
        # names the owner, so the record never attributes an attestation to
        # anybody else.
        if not (access["owner"] or access["backend"]):
            raise refuse(caller, call_id, "attest")
        owner = str(meeting.get("Owner") or "")
        if access["backend"] and not access["owner"]:
            if not owner:
                raise BadRequestError("the meeting has no owner to attest")
            if named and consent.normalize_name(named) != consent.normalize_name(owner):
                raise BadRequestError("AttestedBy must name the meeting's owner")
        attested_by = owner if access["backend"] else caller["username"]
        record = require_started(record)
        record["Attestation"] = {
            "AttestedBy": attested_by,
            "Method": method,
            "Statement": statement,
            "AttestedAt": now,
            "RecordedBy": actor(caller),
        }
        entry = {
            "EventId": uuid.uuid4().hex[:16],
            "ParticipantName": attested_by,
            "Decision": consent.DECISION_CONSENT,
            "Source": consent.SOURCE_ATTESTATION,
            "Text": statement,
            "OccurredAt": now,
            "RecordedAt": now,
            "RecordedBy": actor(caller),
        }
        record["Events"] = consent.compact_events(list(record.get("Events") or []) + [entry])
        return record

    return write_consent(caller, call_id, change)


def add_meeting_consent_evidence(caller: Mapping[str, Any], data: Mapping[str, Any]) -> Dict:
    call_id = data["CallId"]
    if not caller["is_backend"]:
        raise refuse(caller, call_id, "add evidence")
    key = required_text(data.get("S3Key"), "S3Key", MAX_KEY_LENGTH)
    if not key.startswith(evidence_folder(call_id)) or ".." in key:
        raise BadRequestError(f"S3Key must be under {evidence_folder(call_id)}")
    kind = one_of(data.get("Kind"), EVIDENCE_KINDS, "Kind", default="screenshot")

    def change(record, _meeting, _access, now):
        record = require_started(record)
        evidence = list(record.get("Evidence") or [])
        if len(evidence) >= consent.MAX_EVIDENCE:
            raise BadRequestError(f"a meeting keeps at most {consent.MAX_EVIDENCE} evidence images")
        evidence.append(
            {
                "EvidenceId": uuid.uuid4().hex[:16],
                "Kind": kind,
                "S3Key": key,
                "CapturedAt": data.get("CapturedAt") or now,
                "AddedAt": now,
                "Note": consent.clip(data.get("Note"), consent.MAX_TEXT_LENGTH),
            }
        )
        record["Evidence"] = evidence
        return record

    return write_consent(caller, call_id, change)


def find_evidence(record: Mapping[str, Any], evidence_id: str) -> Dict[str, Any]:
    for entry in record.get("Evidence") or []:
        if entry.get("EvidenceId") == evidence_id:
            return entry
    raise BadRequestError(f"the meeting has no evidence {evidence_id}")


def run_verifier(payload: Mapping[str, Any]) -> Dict[str, Any]:
    """The verifier's result for one image, or VerificationFailedError."""
    if not VERIFIER_FUNCTION:
        raise VerificationFailedError("the evidence verifier is not configured")
    response = LAMBDA.invoke(FunctionName=VERIFIER_FUNCTION, Payload=json.dumps(payload).encode())
    answer = json.loads(response["Payload"].read() or b"{}")
    if response.get("FunctionError") or not isinstance(answer, dict):
        LOGGER.error("the evidence verifier failed for %s", payload.get("CallId"))
        raise VerificationFailedError("the evidence could not be checked just now; try again")
    if not answer.get("Ok"):
        raise VerificationFailedError(str(answer.get("Message") or "the evidence was not checked"))
    return dict(answer.get("Verification") or {})


def verify_meeting_consent_evidence(caller: Mapping[str, Any], data: Mapping[str, Any]) -> Dict:
    """Check one evidence image with the vision model, and store the result on it.

    The check is supporting evidence: it is stored on the evidence entry and
    never changes the record's status. When the deployment does not keep
    evidence screenshots, the image is deleted once the result is stored.
    """
    call_id = data["CallId"]
    evidence_id = required_text(data.get("EvidenceId"), "EvidenceId", 64)
    meeting, _access = load(caller, call_id)
    record = require_started(meeting.get("Consent") or None)
    entry = find_evidence(record, evidence_id)
    if entry.get("ImageDeletedAt"):
        raise BadRequestError(
            "the image of this evidence has been deleted, so it cannot be checked"
        )
    settings = consent_settings()
    verification = run_verifier(
        {
            "CallId": call_id,
            "S3Key": entry.get("S3Key"),
            "NoticeText": record.get("NoticeText"),
            "Mode": record.get("Mode"),
            "Rule": record.get("Rule"),
            "Participants": record.get("Participants") or [],
            "ConsentPhrases": settings["consentPhrases"],
            "ObjectionPhrases": settings["objectionPhrases"],
            "ModelId": settings["verifierModelId"],
        }
    )
    verification["VerifiedBy"] = actor(caller)

    def store(record, _meeting, _access, _now):
        record = require_started(record)
        find_evidence(record, evidence_id)["Verification"] = verification
        return record

    answer = write_consent(caller, call_id, store)
    if settings["keepEvidenceScreenshots"]:
        return answer
    try:
        S3.delete_object(Bucket=RECORDINGS_BUCKET, Key=entry["S3Key"])
    except ClientError:
        # The result is stored; the image stays until the meeting is deleted.
        LOGGER.exception("could not delete a checked evidence image of %s", call_id)
        return answer

    def mark_deleted(record, _meeting, _access, now):
        record = require_started(record)
        find_evidence(record, evidence_id)["ImageDeletedAt"] = now
        return record

    return write_consent(caller, call_id, mark_deleted)


MUTATIONS = {
    "startMeetingConsent": start_meeting_consent,
    "recordMeetingConsentNotice": record_meeting_consent_notice,
    "setMeetingConsentParticipants": set_meeting_consent_participants,
    "recordMeetingConsentEvent": record_meeting_consent_event,
    "attestMeetingConsent": attest_meeting_consent,
    "addMeetingConsentEvidence": add_meeting_consent_evidence,
    "verifyMeetingConsentEvidence": verify_meeting_consent_evidence,
}


def handler(event: Dict[str, Any], _context: Any) -> Dict[str, Any]:
    """AppSync Lambda resolver entry point (meetingConsent.request.vtl payload)."""
    field = ((event.get("info") or {}).get("fieldName")) or ""
    arguments = event.get("arguments") or {}
    caller = caller_of(event)
    if field == "getMeetingConsent":
        call_id = arguments.get("CallId")
        if not isinstance(call_id, str) or not call_id:
            raise BadRequestError("CallId is required")
        return get_meeting_consent(caller, arguments)
    operation = MUTATIONS.get(field)
    if operation is None:
        raise BadRequestError(f"unsupported field {field}")
    data = arguments.get("input") or {}
    call_id = data.get("CallId")
    if not isinstance(call_id, str) or not call_id:
        raise BadRequestError("CallId is required")
    return operation(caller, data)
