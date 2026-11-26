#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""Consent given in advance: its scopes, its period, its text, and what it covers.

A signed-in person can consent before a meeting to its being recorded and
transcribed: from the meeting's consent page, or, for a standing consent, on
the My consents page with no meeting named. The consent is kept under the
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
    The organizer is the meeting's owner when the consent is given from a
    meeting's link, or the address the person types when it is given on its
    own; owners are compared ignoring case, as the user pool compares usernames.
``deployment``
    Every meeting on this deployment until a date, within the same longest
    period. Like every other scope, it counts in a meeting only for an attendee
    matched to the person on that meeting's participant list.
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
SCOPE_DEPLOYMENT = "deployment"
SCOPES: Tuple[str, ...] = (SCOPE_MEETING, SCOPE_ORGANIZER, SCOPE_DEPLOYMENT)
#: The scopes a consent given from a meeting's link may have.
LINK_SCOPES: Tuple[str, ...] = (SCOPE_MEETING, SCOPE_ORGANIZER)
#: The scopes a standing consent, given with no meeting named, may have.
STANDING_SCOPES: Tuple[str, ...] = (SCOPE_ORGANIZER, SCOPE_DEPLOYMENT)

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
#: The longest organizer address a standing consent may name.
MAX_ORGANIZER_LENGTH = 254
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ADDRESS = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class InvalidConsent(ValueError):
    """A consent that cannot be given as asked; the message says why."""


def organizer_address(value: Any) -> str:
    """The organizer a standing consent names, as typed: an email address, in lower case.

    LMA's usernames are email addresses, compared ignoring case. The address is
    taken as written and never looked up, so the answer is the same whether
    or not anyone has that address.

    Raises:
        InvalidConsent: it is missing, too long, or not an email address.
    """
    text = value.strip() if isinstance(value, str) else ""
    if not text or len(text) > MAX_ORGANIZER_LENGTH or not _ADDRESS.match(text):
        raise InvalidConsent("Organizer must be the organizer's email address")
    return text.casefold()


def typed_label(key: Any, value: Any) -> Optional[Tuple[str, str]]:
    """The label a standing consent is limited to, as typed, or None for none.

    Raises:
        InvalidConsent: only one of the two is given, or one is too long.
    """
    key = key.strip() if isinstance(key, str) else ""
    value = value.strip() if isinstance(value, str) else ""
    if not key and not value:
        return None
    if not key or not value:
        raise InvalidConsent("A label needs both LabelKey and LabelValue")
    if len(key) > MAX_LABEL_LENGTH or len(value) > MAX_LABEL_LENGTH:
        raise InvalidConsent(
            f"LabelKey and LabelValue may be at most {MAX_LABEL_LENGTH} characters"
        )
    return (key, value)


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

    ``organizer`` and ``deployment``: the end (23:59:59 UTC) of the ``YYYY-MM-DD`` date asked for,
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
    if scope == SCOPE_DEPLOYMENT:
        phrase = "every meeting on this Live Meeting Assistant deployment that I am listed for"
        if until is not None:
            phrase += f", until the end of {iso(until)[:10]} (UTC)"
        return phrase
    phrase = "every meeting they organize"
    if label:
        phrase += f" that is labelled {label[0]}: {label[1]}"
    if until is not None:
        phrase += f", until the end of {iso(until)[:10]} (UTC)"
    return phrase


#: ``{organizer}`` in a consent for every meeting on the deployment.
EACH_ORGANIZER = "the organizer of each meeting"
#: ``{meeting}`` in a standing consent given with no meeting named.
COVERED_MEETINGS = "the meetings this consent covers"


def render_text(
    template: Any, *, organizer: str, meeting_name: str, scope: str, standing: bool = False
) -> str:
    """The text the person agrees to, with its placeholders filled in.

    ``standing`` is a consent given with no meeting named: ``{meeting}`` then
    reads as the meetings it covers. Any other text in braces is left as
    written, as the meeting notices do.
    """
    text = template if isinstance(template, str) and template.strip() else DEFAULT_TEXT
    values = {
        "{organizer}": organizer or "The meeting organizer",
        "{meeting}": meeting_name or (COVERED_MEETINGS if standing else "this meeting"),
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
    meeting whose owner is that organizer, compared ignoring case, as the user
    pool compares usernames, and, when it
    names a label, that carries that label (key and value compared exactly). A
    ``deployment`` consent counts for every meeting.
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
        owner = meeting.get("Owner")
        if not isinstance(organizer, str) or not organizer or not isinstance(owner, str):
            return False
        if organizer.casefold() != owner.casefold():
            return False
        key, value = consent.get("LabelKey"), consent.get("LabelValue")
        if key:
            return (key, value or "") in _labels(meeting.get("Labels"))
        return True
    return scope == SCOPE_DEPLOYMENT


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
    if a.get("Scope") == SCOPE_DEPLOYMENT:
        return True
    return (
        str(a.get("Organizer") or "").casefold() == str(b.get("Organizer") or "").casefold()
        and (a.get("LabelKey") or "") == (b.get("LabelKey") or "")
        and (a.get("LabelValue") or "") == (b.get("LabelValue") or "")
    )


def match_listed(
    present: Iterable[Any],
    listed: Iterable[Mapping[str, Any]],
    accounts: Optional[Mapping[str, Mapping[str, Any]]] = None,
) -> Dict[str, Dict[str, str]]:
    """The attendees present matched to the participant list, by display name.

    Each listed person (one per address, compared ignoring case) goes by the
    name their list entry gives them and, when ``accounts`` has their address
    in lower case, by their account's names
    (:func:`lma_consent.account_names.account_names`: ``display_name`` and
    ``short_names``). Answers each present display name that equals a name of
    exactly one listed person, with that person's list ``name``, ``email`` and
    ``source``, which of their names it was (``list``, ``display_name`` or
    ``short_name``, in that order when several are the same). Names are
    compared as replies are (:func:`lma_consent.rules.normalize_name`), so case
    and spacing do not matter. Nobody is matched when the name is empty, when
    two listed people go by that name, when two attendees present show it, or
    when two attendees each show one of the same person's names:
    a display name is what the platform shows, and a name shared by two people
    says nothing about which of them is which. The basis of every match is
    :data:`lma_consent.rules.MATCH_DISPLAY_NAME`.
    """
    people: Dict[str, Dict[str, str]] = {}
    for person in listed or []:
        if not isinstance(person, Mapping):
            continue
        email = person.get("email")
        if isinstance(email, str) and email.strip():
            # A list holds an address once (normalize_participants in
            # lma_meeting_preparation keeps the first entry); should one hold
            # it twice, the first entry is the person.
            people.setdefault(
                email.strip().casefold(),
                {"name": str(person.get("name") or "").strip(), "email": email.strip()},
            )
    by_name: Dict[str, Dict[str, str]] = {}
    for address, person in people.items():
        account = (accounts or {}).get(address) or {}
        names = [(person["name"], "list"), (account.get("display_name"), "display_name")]
        names += [(short, "short_name") for short in account.get("short_names") or []]
        for name, source in names:
            key = normalize_name(name)
            if key:
                # The first of the person's names with this key decides the source.
                by_name.setdefault(key, {}).setdefault(address, source)
    seen: Dict[str, List[str]] = {}
    for name in present or []:
        key = normalize_name(name)
        if key:
            seen.setdefault(key, []).append(str(name))
    # Every name shown counts as showing each person who goes by it, whether
    # or not it matches: two attendees showing two names of one person, or a
    # name of that person shared with someone else, means neither is that person.
    shown: Dict[str, set] = {}
    for key in seen:
        for address in by_name.get(key) or {}:
            shown.setdefault(address, set()).add(key)
    matches: Dict[str, Dict[str, str]] = {}
    for key, names in seen.items():
        holders = by_name.get(key) or {}
        if len(holders) == 1 and len(names) == 1:
            [(address, source)] = holders.items()
            if shown[address] == {key}:
                matches[names[0]] = {**people[address], "source": source}
    return matches


# ── a request to be added to a meeting's participant list ───────────────────

_MARKDOWN_SPECIAL = re.compile(r"([\\`*_{}\[\]()#+\-.!|<>~])")

#: The meeting notification a request to be added is shown as.
REQUEST_NOTIFICATION_TYPE = "participant_list_request"


def request_dedup_key(email: str) -> str:
    """The notification's dedup key for one address: a later request updates it."""
    digest = hashlib.sha256(email.strip().casefold().encode()).hexdigest()[:16]
    return f"{REQUEST_NOTIFICATION_TYPE}:{digest}"


def participant_list_request_notification(email: str, name: str = "") -> Dict[str, Any]:
    """The keyword arguments of lma_notifications.publish_meeting_notification for a request.

    The same for a request made on a meeting and for one made on a Virtual
    Participant before its meeting existed and published to the meeting when
    it was created, so one address is one notification on the meeting.
    """
    address = email.strip().casefold()
    who = f"{name} <{address}>" if name else address
    shown = _MARKDOWN_SPECIAL.sub(r"\\\1", who)
    return {
        "category": "meeting",
        "type": REQUEST_NOTIFICATION_TYPE,
        "title": "Asked to be added to the participant list",
        "body": (
            f"**{shown}** opened this meeting's consent link and is "
            "not on its participant list. To let them give consent in advance, add them in the "
            "meeting's **Participants** section. Nothing about their consent is recorded until "
            "then."
        ),
        "severity": "suggestion",
        "dedup_key": request_dedup_key(address),
        "actions": [{"label": "Copy name and address", "kind": "copy", "payload": {"text": who}}],
        "source": "advance-consent",
    }
