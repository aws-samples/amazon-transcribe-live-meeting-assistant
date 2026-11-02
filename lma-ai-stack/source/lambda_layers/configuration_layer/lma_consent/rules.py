#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""The rules of meeting consent, with no AWS in them.

A meeting's consent record (``Consent`` on the meeting row) holds the mode it
runs in, the rule an opt-in meeting is granted by, and what has happened: the
notices posted, the participants present, each participant's replies, the
owner's attestation and the evidence kept. Everything that decides something
from that record lives here, so the resolver that writes it and the capture
clients' gate decide the same way. See docs/meeting-consent.md.

Modes, from least to most strict:

``off``
    No notice and no consent. The status is ``not_required`` and capture is
    allowed.
``notify``
    Opt-out. A notice is posted and capture proceeds; an objection from any
    participant stops it. ``pending`` until the notice is recorded as posted,
    then ``granted``; ``objected`` while any participant's latest reply is an
    objection.
``opt_in``
    Capture is withheld until the consent rule is met:

    * ``each_participant`` -- every participant listed as present has replied
      with consent, and at least one participant is listed;
    * ``owner_attests`` -- the meeting's owner has attested that everyone
      agreed.

    ``objected`` while any participant's latest reply is an objection, whatever
    the rule, so an attestation never outweighs an objection.

A participant's *latest* reply is the one that counts, so somebody who objected
and then consented has consented. Replies are matched to participants by display
name, compared case-insensitively with spacing collapsed; a display name is what
the meeting platform shows, not a verified identity.
"""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

# Kept free of boto3 and of lma_config so a capture client's Python, a test or
# a script can import the rules without the configuration store.

MODE_OFF = "off"
MODE_NOTIFY = "notify"
MODE_OPT_IN = "opt_in"
#: From least to most strict.
MODES: Tuple[str, ...] = (MODE_OFF, MODE_NOTIFY, MODE_OPT_IN)

RULE_EACH_PARTICIPANT = "each_participant"
RULE_OWNER_ATTESTS = "owner_attests"
#: From least to most strict.
RULES: Tuple[str, ...] = (RULE_OWNER_ATTESTS, RULE_EACH_PARTICIPANT)

STATUS_PENDING = "pending"
STATUS_GRANTED = "granted"
STATUS_OBJECTED = "objected"
STATUS_NOT_REQUIRED = "not_required"
STATUSES: Tuple[str, ...] = (STATUS_PENDING, STATUS_GRANTED, STATUS_OBJECTED, STATUS_NOT_REQUIRED)

DECISION_CONSENT = "consent"
DECISION_OBJECTION = "objection"
DECISIONS: Tuple[str, ...] = (DECISION_CONSENT, DECISION_OBJECTION)

#: Where a reply was read: the meeting chat, the meeting's speech, or entered
#: on the meeting page by a signed-in user.
EVENT_SOURCES: Tuple[str, ...] = ("chat", "voice", "ui")
#: The source of the timeline entry an attestation adds. Not a participant's
#: reply, so it never counts towards ``each_participant``.
SOURCE_ATTESTATION = "attestation"

NOTICE_CHANNELS: Tuple[str, ...] = ("chat", "voice", "banner", "invite")
ATTESTATION_METHODS: Tuple[str, ...] = ("verbal", "chat", "invite_terms")

#: The deployment settings when the configuration section holds nothing. The
#: seeder's shipped defaults (configuration_seeder/defaults/MeetingConsent.json)
#: carry the same values; a test holds the two together.
DEFAULT_SETTINGS: Dict[str, Any] = {
    "mode": MODE_OFF,
    "consentRule": RULE_EACH_PARTICIPANT,
    "notifyNoticeText": (
        "{LMA_USER} is using Live Meeting Assistant to record and transcribe this meeting "
        '({meeting}). If you do not agree, reply "{objection_phrase}" in the chat or say '
        "so, and recording and transcription will stop."
    ),
    "optInNoticeText": (
        "{LMA_USER} would like to record and transcribe this meeting ({meeting}) with Live "
        "Meeting Assistant. Nothing is recorded or transcribed until everyone has agreed. "
        'Reply "{consent_phrase}" to agree, or "{objection_phrase}" to decline.'
    ),
    "consentPhrases": "I consent, I agree, I accept",
    "objectionPhrases": (
        "I object, I do not consent, I don't consent, I do not agree, I don't agree, "
        "do not record, don't record, stop recording"
    ),
    "allowPerMeetingOverride": True,
    "keepEvidenceScreenshots": True,
    "verifierModelId": "amazon.nova-lite-v1:0",
}

# Bounds on each part of one meeting's record. They bound each list; the record
# as a whole is held to MAX_RECORD_BYTES by fit_to_budget, because the record
# shares one DynamoDB item (400 KB at most) with the rest of the meeting row --
# its summary, agenda and speaker names among them.
MAX_NAME_LENGTH = 100
MAX_TEXT_LENGTH = 500
MAX_NOTICE_LENGTH = 2000
MAX_PARTICIPANTS = 250
#: Everyone ever listed as present; the first this many are kept.
MAX_PARTICIPANTS_SEEN = 500
MAX_EVENTS = 300
MAX_NOTICES = 10
MAX_EVIDENCE = 30
MAX_STATUS_HISTORY = 100
#: The most a record may take, serialised as JSON: under two fifths of the item
#: limit, leaving the rest of the row (a long summary is tens of kilobytes) the
#: remainder. Every list at its cap fits within it once fit_to_budget has shrunk
#: what it may; test_rules holds to that.
MAX_RECORD_BYTES = 150_000

_SPACES = re.compile(r"\s+")
_NOT_WORD = re.compile(r"[^\w']+")


def normalize_name(name: Any) -> str:
    """A display name as replies are matched to participants by.

    Unicode-normalised, case-folded, with runs of spacing collapsed, so "Ana
    Silva", "ana  silva" and "ANA SILVA" are one participant. Not an identity:
    two people showing the same name are one participant here.
    """
    if not isinstance(name, str):
        return ""
    folded = unicodedata.normalize("NFKC", name).casefold()
    return _SPACES.sub(" ", folded).strip()


def split_phrases(value: Any) -> List[str]:
    """Comma-separated phrases as a list, trimmed, empty ones dropped."""
    if isinstance(value, (list, tuple)):
        parts = [str(part) for part in value]
    elif isinstance(value, str):
        parts = value.split(",")
    else:
        return []
    return [part.strip() for part in parts if part.strip()]


def _words(text: str) -> str:
    """Text reduced to lowercase words separated by single spaces.

    Curly apostrophes become straight ones, so "don’t" matches "don't"; every
    other run of punctuation or spacing becomes one space.
    """
    folded = unicodedata.normalize("NFKC", text).casefold().replace("’", "'")
    return " ".join(_NOT_WORD.sub(" ", folded).split())


def _contains_phrase(words: str, phrase: str) -> bool:
    target = _words(phrase)
    return bool(target) and f" {target} " in f" {words} "


def classify_reply(
    text: Any, consent_phrases: Iterable[str], objection_phrases: Iterable[str]
) -> Optional[str]:
    """``objection``, ``consent`` or ``None`` for one chat message or utterance.

    A message is an objection when it contains an objection phrase as whole
    words, and otherwise consent when it contains a consent phrase. Objections
    are looked for first, so "I do not consent" is an objection although it
    contains "consent". Case, punctuation and spacing are ignored.
    """
    if not isinstance(text, str) or not text.strip():
        return None
    words = _words(text)
    if any(_contains_phrase(words, phrase) for phrase in objection_phrases):
        return DECISION_OBJECTION
    if any(_contains_phrase(words, phrase) for phrase in consent_phrases):
        return DECISION_CONSENT
    return None


def render_notice(
    template: str,
    *,
    lma_user: str = "",
    meeting: str = "",
    consent_phrase: str = "",
    objection_phrase: str = "",
) -> str:
    """The notice text with its placeholders filled in.

    ``{LMA_USER}`` is the person using LMA in the meeting, ``{meeting}`` the
    meeting's name, and ``{consent_phrase}`` / ``{objection_phrase}`` the first
    phrase of each list. Any other text in braces is left as written.
    """
    values = {
        "{LMA_USER}": lma_user or "The meeting organizer",
        "{meeting}": meeting or "this meeting",
        "{consent_phrase}": consent_phrase,
        "{objection_phrase}": objection_phrase,
    }
    pattern = re.compile("|".join(re.escape(key) for key in values))
    rendered = pattern.sub(lambda match: values[match.group(0)], template or "")
    return rendered[:MAX_NOTICE_LENGTH]


def effective_settings(values: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """The deployment's consent settings: the section's values over the defaults.

    A value of the wrong type, or a mode or rule not in the lists, is replaced
    by the default rather than trusted, because the mode decides whether a
    meeting may be captured. The phrase lists are returned as lists.
    """
    merged = dict(DEFAULT_SETTINGS)
    for key, default in DEFAULT_SETTINGS.items():
        value = (values or {}).get(key)
        if value is not None and isinstance(value, type(default)):
            merged[key] = value
    if merged["mode"] not in MODES:
        merged["mode"] = DEFAULT_SETTINGS["mode"]
    if merged["consentRule"] not in RULES:
        merged["consentRule"] = DEFAULT_SETTINGS["consentRule"]
    merged["consentPhrases"] = split_phrases(merged["consentPhrases"])
    merged["objectionPhrases"] = split_phrases(merged["objectionPhrases"])
    return merged


def strictness(mode: str, rule: Optional[str]) -> Tuple[int, int]:
    """How strict a mode and rule are, for comparing a meeting's with the default."""
    mode_rank = MODES.index(mode) if mode in MODES else 0
    rule_rank = RULES.index(rule) if mode == MODE_OPT_IN and rule in RULES else -1
    return (mode_rank, rule_rank)


def override_permitted(
    settings: Mapping[str, Any], mode: str, rule: Optional[str]
) -> Tuple[bool, str]:
    """Whether a meeting may run in ``mode`` / ``rule`` under these settings.

    Always, when the deployment allows a per-meeting choice. Otherwise only the
    deployment's own mode and rule, or a stricter one: a meeting may ask for
    more consent than the deployment requires, never less.
    """
    if settings.get("allowPerMeetingOverride", True):
        return True, ""
    default = strictness(settings["mode"], settings["consentRule"])
    if strictness(mode, rule) >= default:
        return True, ""
    return False, (
        "this deployment does not allow a meeting to use less consent than its "
        f"default ({settings['mode']}"
        + (f", {settings['consentRule']}" if settings["mode"] == MODE_OPT_IN else "")
        + ")"
    )


def latest_decisions(events: Sequence[Mapping[str, Any]]) -> Dict[str, str]:
    """Each participant's latest reply, keyed by normalised name.

    Attestation entries are not a participant's reply and are left out. Events
    are taken in the order they were recorded, which the resolver serialises
    with a version check, rather than by the ``OccurredAt`` a client reports:
    clients' clocks and timestamp formats differ, and the order of recording is
    the one every reader of the record sees.
    """
    latest: Dict[str, str] = {}
    for event in events or []:
        if event.get("Source") == SOURCE_ATTESTATION:
            continue
        name = normalize_name(event.get("ParticipantName"))
        decision = event.get("Decision")
        if name and decision in DECISIONS:
            latest[name] = decision
    return latest


def _deciding_positions(events: Sequence[Mapping[str, Any]]) -> set:
    """Each participant's latest reply and the latest attestation entry."""
    keep, seen = set(), set()
    for position in range(len(events) - 1, -1, -1):
        event = events[position]
        if event.get("Source") == SOURCE_ATTESTATION:
            key: Tuple[str, ...] = ("attestation",)
        else:
            key = ("reply", normalize_name(event.get("ParticipantName")))
        if key not in seen:
            seen.add(key)
            keep.add(position)
    return keep


def compact_events(events: Sequence[Mapping[str, Any]], limit: int = MAX_EVENTS) -> List[Any]:
    """At most ``limit`` events, never losing one the status depends on.

    Each participant's latest reply and the latest attestation entry are kept
    whatever their age; the rest of the room is filled with the most recent
    other events. Order is preserved. So an objection is never dropped to make
    room. Replies can come from more distinct names than ``limit`` -- anybody
    may reply, listed or not -- and then more than ``limit`` events are kept;
    :func:`fit_to_budget` bounds the record's size whatever their number.
    """
    events = list(events or [])
    if len(events) <= limit:
        return events
    keep = _deciding_positions(events)
    for position in range(len(events) - 1, -1, -1):
        if len(keep) >= limit:
            break
        keep.add(position)
    return [event for position, event in enumerate(events) if position in keep]


class RecordFull(Exception):
    """The record cannot be brought within MAX_RECORD_BYTES without losing what
    its status depends on."""


def record_size(record: Mapping[str, Any]) -> int:
    """The record's size serialised as compact JSON, in bytes.

    Close to, and slightly above, what DynamoDB counts for the same map, which
    is what the budget is for.
    """
    return len(json.dumps(record, default=str, separators=(",", ":")).encode("utf-8"))


#: What a reply keeps when nothing else is left to shrink: enough to decide the
#: status and to say when and where it was made.
_ESSENTIAL_EVENT_FIELDS = ("ParticipantName", "Decision", "Source", "OccurredAt")


def fit_to_budget(record: Dict[str, Any], limit: int = MAX_RECORD_BYTES) -> Dict[str, Any]:
    """``record`` shrunk, if it has to be, to at most ``limit`` bytes.

    Never drops anything the status depends on -- a participant's latest reply,
    the attestation, the participants present -- and shrinks in this order,
    stopping as soon as the record fits:

    1. replies that are no longer anybody's latest, oldest first;
    2. the words of replies, oldest first, keeping who replied and how;
    3. the oldest status history entries, keeping the latest ten;
    4. the notices posted between the first and the latest;
    5. the names once listed but not present now;
    6. the replies the evidence check read in each image, keeping its verdict;
    7. every reply's other fields (its id, who recorded it and when), and each
       evidence image's note and check summary.

    Raises:
        RecordFull: the record is still over ``limit`` after all of that.
    """
    size = record_size(record)
    if size <= limit:
        return record

    def piece(value: Any) -> int:
        return record_size({"v": value}) - 6  # the value's own bytes, with its comma

    # 1. Earlier replies, oldest first, removed until the estimate fits.
    events = list(record.get("Events") or [])
    deciding = _deciding_positions(events)
    dropped = set()
    for position in range(len(events)):
        if size <= limit:
            break
        if position not in deciding:
            dropped.add(position)
            size -= piece(events[position])
    record["Events"] = [e for p, e in enumerate(events) if p not in dropped]
    size = record_size(record)
    # 2. The words of the replies kept, oldest first.
    for event in record["Events"]:
        if size <= limit:
            break
        if event.get("Text"):
            size -= piece(event["Text"]) - piece(None)
            event["Text"] = None
    size = record_size(record)
    if size <= limit:
        return record
    # 3-5. The older history, the middle notices, the names no longer present.
    record["StatusHistory"] = list(record.get("StatusHistory") or [])[-10:]
    notices = list(record.get("Notices") or [])
    if len(notices) > 2:
        record["Notices"] = [notices[0], notices[-1]]
    present = {normalize_name(name) for name in record.get("Participants") or []}
    record["ParticipantsSeen"] = [
        name for name in record.get("ParticipantsSeen") or [] if normalize_name(name) in present
    ]
    if record_size(record) <= limit:
        return record
    # 6. What the evidence check read in each image; its verdict stays.
    for entry in record.get("Evidence") or []:
        verification = entry.get("Verification")
        if isinstance(verification, dict) and verification.get("Replies"):
            verification["Replies"] = []
    if record_size(record) <= limit:
        return record
    # 7. The replies kept, down to who, how, where and when.
    record["Events"] = [
        {key: event.get(key) for key in _ESSENTIAL_EVENT_FIELDS} for event in record["Events"]
    ]
    for entry in record.get("Evidence") or []:
        entry["Note"] = None
        if isinstance(entry.get("Verification"), dict):
            entry["Verification"]["Summary"] = None
    if record_size(record) <= limit:
        return record
    raise RecordFull(
        f"the meeting's consent record would exceed {limit} bytes even without the "
        "earlier replies, their words and the older history"
    )


def evaluate(record: Optional[Mapping[str, Any]]) -> Tuple[str, bool, str]:
    """``(status, capture_allowed, reason)`` for one consent record.

    ``reason`` is a short sentence for the status history and the meeting page.
    """
    if not record:
        return STATUS_NOT_REQUIRED, True, "consent is not in use for this meeting"
    mode = record.get("Mode")
    if mode == MODE_OFF:
        return STATUS_NOT_REQUIRED, True, "consent is not required"
    if mode not in MODES:
        # Never written by this module; a record it cannot judge is not consent.
        return STATUS_PENDING, False, f"the consent mode {mode!r} is not recognised"
    decisions = latest_decisions(record.get("Events") or [])
    objectors = sorted(
        name for name, decision in decisions.items() if decision == DECISION_OBJECTION
    )
    if objectors:
        noun = "participant" if len(objectors) == 1 else "participants"
        return STATUS_OBJECTED, False, f"{len(objectors)} {noun} objected"
    if mode == MODE_NOTIFY:
        if record.get("NoticePostedAt"):
            return STATUS_GRANTED, True, "the notice was posted and nobody has objected"
        return STATUS_PENDING, True, "the notice has not been posted yet; nobody has objected"
    rule = record.get("Rule")
    if rule == RULE_OWNER_ATTESTS:
        if record.get("Attestation"):
            return STATUS_GRANTED, True, "the meeting owner attested that everyone agreed"
        return STATUS_PENDING, False, "waiting for the meeting owner to attest"
    participants = {normalize_name(name) for name in record.get("Participants") or []}
    participants.discard("")
    if not participants:
        return STATUS_PENDING, False, "no participants are listed yet"
    waiting = sorted(name for name in participants if decisions.get(name) != DECISION_CONSENT)
    if waiting:
        noun = "participant has" if len(waiting) == 1 else "participants have"
        return STATUS_PENDING, False, f"{len(waiting)} {noun} not consented yet"
    return STATUS_GRANTED, True, "every participant consented"


def capture_allowed(record: Optional[Mapping[str, Any]], deployment_mode: str = MODE_OFF) -> bool:
    """May the meeting be recorded and transcribed now?

    The gate the capture clients apply:

    * ``opt_in`` -- only while the status is ``granted``;
    * ``notify`` -- unless the status is ``objected``;
    * ``off`` -- always.

    A meeting with no consent record is judged by ``deployment_mode``: in an
    ``opt_in`` deployment it may not be captured until its consent has been
    started and granted, because the absence of a record is not consent. A
    record whose mode is not one of :data:`MODES` is never captured, as
    :func:`evaluate` says, so the gate and the record's own ``CaptureAllowed``
    agree.
    """
    if not record or not record.get("Mode"):
        return deployment_mode != MODE_OPT_IN
    _status, allowed, _reason = evaluate(record)
    return allowed


def apply_evaluation(record: Dict[str, Any], now_iso: str) -> Dict[str, Any]:
    """Set ``Status`` and ``CaptureAllowed`` on ``record`` and log a change.

    A change of status, or of whether capture is allowed, adds an entry to
    ``StatusHistory`` (the newest kept), so the record shows when capture was
    stopped and resumed as well as why.
    """
    status, allowed, reason = evaluate(record)
    changed = record.get("Status") != status or record.get("CaptureAllowed") != allowed
    record["Status"] = status
    record["CaptureAllowed"] = allowed
    if changed:
        history = list(record.get("StatusHistory") or [])
        history.append(
            {"Status": status, "CaptureAllowed": allowed, "At": now_iso, "Reason": reason}
        )
        record["StatusHistory"] = history[-MAX_STATUS_HISTORY:]
    return record


def clip(value: Any, limit: int) -> Optional[str]:
    """A string argument trimmed and cut to ``limit`` characters; None when empty."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value[:limit] if value else None
