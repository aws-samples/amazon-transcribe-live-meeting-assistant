#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""Consent given in advance: its scopes, its period, its text, and what it covers.

A signed-in person can consent before a meeting to its being recorded and
transcribed, from the meeting's consent page. The consent is kept under the
person's email address, with the scope it was given for, when it was given,
until when it counts, when it was withdrawn, and the exact text they agreed to.
Everything here is free of AWS, so every reader of these consents decides what
one covers by the same rules. See docs/meeting-consent.md#consent-given-in-advance.

Scopes:

``meeting``
    One meeting, named by its CallId, or by the Virtual Participant that will
    create it (its id) when the meeting does not exist yet.
``organizer``
    Every meeting the named organizer (the meeting's owner in LMA) holds until
    a date, at most a year ahead, optionally only those that carry one label.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from .rules import DEFAULT_SETTINGS, normalize_name

#: The source a consent given in advance is recorded with.
SOURCE_PRE_MEETING = "pre_meeting"

SCOPE_MEETING = "meeting"
SCOPE_ORGANIZER = "organizer"
SCOPES: Tuple[str, ...] = (SCOPE_MEETING, SCOPE_ORGANIZER)

STATUS_ACTIVE = "active"
STATUS_WITHDRAWN = "withdrawn"
STATUS_EXPIRED = "expired"

#: The longest a consent given in advance may count for, whatever the setting.
MAX_DAYS_LIMIT = 365

#: The text a person agrees to, when the Meeting Consent section holds none.
#: ``{organizer}`` is the meeting's owner in LMA, ``{meeting}`` its name, and
#: ``{scope}`` what the consent covers (see :func:`scope_phrase`).
DEFAULT_TEXT: str = DEFAULT_SETTINGS["advanceConsentText"]

MAX_TEXT_LENGTH = 2000
MAX_NAME_LENGTH = 200
MAX_LABEL_LENGTH = 256
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class InvalidConsent(ValueError):
    """A consent that cannot be given as asked; the message says why."""


def parse_time(value: Any) -> Optional[datetime]:
    """An ISO 8601 time as an aware UTC datetime, or None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def iso(moment: datetime) -> str:
    """A UTC time as the records hold it: ISO 8601 with a Z, to the second."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def max_days(setting: Any) -> int:
    """The Meeting Consent setting's longest period, held to 1..365 days."""
    if isinstance(setting, bool) or not isinstance(setting, int):
        return MAX_DAYS_LIMIT
    return max(1, min(MAX_DAYS_LIMIT, setting))


def valid_until(scope: str, requested: Any, now: datetime, days: int) -> datetime:
    """Until when a consent given ``now`` counts.

    ``organizer``: the end (23:59:59 UTC) of the ``YYYY-MM-DD`` date asked for,
    which must be today or later and at most ``days`` days ahead. ``meeting``:
    ``days`` days from now, which is longer than any meeting is scheduled ahead
    in practice; a date asked for is used when it is sooner.

    Raises:
        InvalidConsent: the date is missing, malformed, past or too far ahead.
    """
    latest = now + timedelta(days=days)
    if scope == SCOPE_MEETING and not requested:
        return latest
    if not isinstance(requested, str) or not _DATE.match(requested):
        raise InvalidConsent("ValidUntil must be a date written YYYY-MM-DD")
    try:
        day = datetime.strptime(requested, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError as error:
        raise InvalidConsent("ValidUntil must be a date written YYYY-MM-DD") from error
    end = day + timedelta(days=1) - timedelta(seconds=1)
    if end < now:
        raise InvalidConsent("ValidUntil must be today or later")
    if day.date() > latest.date():
        raise InvalidConsent(
            f"ValidUntil may be at most {days} days ahead ({iso(latest)[:10]} at the latest)"
        )
    return min(end, latest) if scope == SCOPE_MEETING else end


def scope_phrase(
    scope: str,
    *,
    meeting_name: str = "",
    until: Optional[datetime] = None,
    label: Optional[Tuple[str, str]] = None,
) -> str:
    """What a consent covers, in words, as the text the person agrees to says it."""
    if scope == SCOPE_MEETING:
        return f'the meeting "{meeting_name}"' if meeting_name else "this meeting"
    phrase = "every meeting they organize"
    if label:
        phrase += f" that is labelled {label[0]}: {label[1]}"
    if until is not None:
        phrase += f", until the end of {iso(until)[:10]} (UTC)"
    return phrase


def render_text(template: Any, *, organizer: str, meeting_name: str, scope: str) -> str:
    """The text the person agrees to, with its placeholders filled in.

    Any other text in braces is left as written, as the meeting notices do.
    """
    text = template if isinstance(template, str) and template.strip() else DEFAULT_TEXT
    values = {
        "{organizer}": organizer or "The meeting organizer",
        "{meeting}": meeting_name or "this meeting",
        "{scope}": scope,
    }
    pattern = re.compile("|".join(re.escape(key) for key in values))
    return pattern.sub(lambda match: values[match.group(0)], text)[:MAX_TEXT_LENGTH]


def text_hash(text: str) -> str:
    """The SHA-256 of the exact text agreed to, in hex."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def text_version(template: Any) -> str:
    """Which version of the configured text a consent was given under.

    The first 12 hex digits of the SHA-256 of the template as configured
    (placeholders unfilled), so every consent given under one wording carries
    one version, and a change of wording a new one.
    """
    text = template if isinstance(template, str) and template.strip() else DEFAULT_TEXT
    return text_hash(text)[:12]


def status_of(consent: Mapping[str, Any], now: datetime) -> str:
    """``active``, ``withdrawn`` or ``expired`` at ``now``."""
    if consent.get("WithdrawnAt"):
        return STATUS_WITHDRAWN
    until = parse_time(consent.get("ValidUntil"))
    if until is None or until < now:
        return STATUS_EXPIRED
    return STATUS_ACTIVE


def _labels(value: Any) -> List[Tuple[str, str]]:
    out = []
    for entry in value or []:
        if isinstance(entry, Mapping):
            key = entry.get("Key") or entry.get("key")
            label_value = entry.get("Value") or entry.get("value")
            if isinstance(key, str) and isinstance(label_value, str):
                out.append((key, label_value))
    return out


def covers(consent: Mapping[str, Any], meeting: Mapping[str, Any], now: datetime) -> bool:
    """Whether a consent given in advance counts for ``meeting`` at ``now``.

    ``meeting`` holds the ``CallId``, the ``VirtualParticipantId`` that created
    it (when one did), its ``Owner`` and its ``Labels``. A withdrawn or expired
    consent never counts; one given after ``now`` does not either. A
    ``meeting`` consent counts for the meeting it names, by CallId or by the
    Virtual Participant that created it. An ``organizer`` consent counts for any
    meeting whose owner is that organizer, compared exactly, as LMA compares
    owners, and, when it names a label, that carries that label (key and value
    compared exactly).
    """
    if status_of(consent, now) != STATUS_ACTIVE:
        return False
    granted = parse_time(consent.get("GrantedAt"))
    if granted is None or granted > now:
        return False
    scope = consent.get("Scope")
    if scope == SCOPE_MEETING:
        call_id = consent.get("CallId")
        vp_id = consent.get("VirtualParticipantId")
        return bool(
            (call_id and call_id == meeting.get("CallId"))
            or (vp_id and vp_id == meeting.get("VirtualParticipantId"))
        )
    if scope == SCOPE_ORGANIZER:
        organizer = consent.get("Organizer")
        if not organizer or organizer != meeting.get("Owner"):
            return False
        key, value = consent.get("LabelKey"), consent.get("LabelValue")
        if key:
            return (key, value or "") in _labels(meeting.get("Labels"))
        return True
    return False


def covering(
    consents: Iterable[Mapping[str, Any]], meeting: Mapping[str, Any], now: datetime
) -> Optional[Dict[str, Any]]:
    """The consent that counts for ``meeting``, the most recently given, or None."""
    best: Optional[Mapping[str, Any]] = None
    for consent in consents:
        if covers(consent, meeting, now) and (
            best is None or str(consent.get("GrantedAt")) > str(best.get("GrantedAt"))
        ):
            best = consent
    return dict(best) if best is not None else None


def same_scope(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    """Whether two consents are for the same thing, so a new one replaces the other."""
    if a.get("Scope") != b.get("Scope"):
        return False
    if a.get("Scope") == SCOPE_MEETING:
        return bool(
            (a.get("CallId") and a.get("CallId") == b.get("CallId"))
            or (
                a.get("VirtualParticipantId")
                and a.get("VirtualParticipantId") == b.get("VirtualParticipantId")
            )
        )
    return (
        a.get("Organizer") == b.get("Organizer")
        and (a.get("LabelKey") or "") == (b.get("LabelKey") or "")
        and (a.get("LabelValue") or "") == (b.get("LabelValue") or "")
    )


def match_listed(
    present: Iterable[Any], listed: Iterable[Mapping[str, Any]]
) -> Dict[str, Dict[str, str]]:
    """The attendees present matched to the participant list, by display name.

    Answers each present display name that matches exactly one listed person,
    with that person's ``name`` and ``email``. Names are compared as replies
    are (:func:`lma_consent.rules.normalize_name`), so case and spacing do not
    matter. Nobody is matched when the name is empty, when two listed people
    have that name, or when two attendees present show it: a display name is
    what the platform shows, and a name shared by two people says nothing about
    which of them is which. The basis of every match is
    :data:`lma_consent.rules.MATCH_DISPLAY_NAME`.
    """
    by_name: Dict[str, List[Dict[str, str]]] = {}
    for person in listed or []:
        if not isinstance(person, Mapping):
            continue
        email = person.get("email")
        key = normalize_name(person.get("name"))
        if key and isinstance(email, str) and email.strip():
            by_name.setdefault(key, []).append(
                {"name": str(person.get("name") or "").strip(), "email": email.strip()}
            )
    seen: Dict[str, List[str]] = {}
    for name in present or []:
        key = normalize_name(name)
        if key:
            seen.setdefault(key, []).append(str(name))
    matches: Dict[str, Dict[str, str]] = {}
    for key, names in seen.items():
        people = by_name.get(key) or []
        if len(people) == 1 and len(names) == 1:
            matches[names[0]] = people[0]
    return matches
