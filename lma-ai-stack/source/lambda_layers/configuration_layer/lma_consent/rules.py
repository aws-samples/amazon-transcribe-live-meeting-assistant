#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
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
#: notice texts carry no reply instruction: each capture client adds its own
#: (the Virtual Participant posts a line giving the "LMA, <phrase>" form after
#: the notice), so a customised text is posted as written. The
#: seeder's shipped defaults (configuration_seeder/defaults/MeetingConsent.json)
#: carry the same values; a test holds the two together.
DEFAULT_SETTINGS: Dict[str, Any] = {
    "mode": MODE_OFF,
    "consentRule": RULE_EACH_PARTICIPANT,
    "notifyNoticeText": (
        "{LMA_USER} is using Live Meeting Assistant to record and transcribe this meeting "
        "({meeting}). If you do not agree, say so, and recording and transcription will stop."
    ),
    "optInNoticeText": (
        "{LMA_USER} would like to record and transcribe this meeting ({meeting}) with Live "
        "Meeting Assistant. Nothing is recorded or transcribed until everyone has agreed."
    ),
    "consentPhrases": "I consent, I agree, I accept",
    "objectionPhrases": (
        "I object, I do not consent, I don't consent, I do not agree, I don't agree, "
        "do not record, don't record, stop recording"
    ),
    "allowPerMeetingOverride": True,
    "keepEvidenceScreenshots": True,
    "verifierModelId": "amazon.nova-lite-v1:0",
    # What the WebSocket transcriber does with a streaming client that sends no
    # consent step while the mode is notify or opt_in: "accept_with_warning" or
    # "reject". Read by the transcriber only.
    "clientsWithoutConsent": "accept_with_warning",
    # Read by the Virtual Participant only: whether it leaves the meeting after
    # a participant objects, rather than staying in it without capturing.
    "leaveOnObjection": False,
    # Read by the Virtual Participant only: whether it also reads consent
    # replies and objections said aloud and addressed to LMA, listening on the
    # meeting's transcription engine while consent is awaited.
    "readSpokenReplies": True,
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

#: How a reply was linked to the transcript speaker who gave it
#: (``SpeakerLink.Basis``). ``same_segment``: the reply is that speaker's own
#: final line of the transcript, so the line's channel and speaker as stored
#: are the speaker. Recorded by the capture client that wrote the line, when the
#: reply is recorded; never worked out afterwards from a name.
SPEAKER_LINK_SAME_SEGMENT = "same_segment"
#: ``platform_identity``: a chat reply whose sender the meeting platform gave the
#: same participant id as the transcript speaker's rows carry
#: (``PlatformParticipantId`` on the segment). Recorded by the Virtual
#: Participant on the joins whose platform gives that id with both, naming the
#: speaker most recently written with it; never worked out from a name.
SPEAKER_LINK_PLATFORM_IDENTITY = "platform_identity"
SPEAKER_LINK_BASES: Tuple[str, ...] = (
    SPEAKER_LINK_SAME_SEGMENT,
    SPEAKER_LINK_PLATFORM_IDENTITY,
)
#: A meeting platform's participant id, as a segment and a link carry it.
MAX_PLATFORM_ID_LENGTH = 256
#: The channels a linked line may be on: the meeting's speech.
SPEAKER_LINK_CHANNELS: Tuple[str, ...] = ("REMOTE", "LOCAL")
_STORED_SPEECH_CHANNEL = {"CALLER": "REMOTE", "AGENT": "LOCAL"}
MAX_SEGMENT_ID_LENGTH = 128
#: A stored speaker: a display name with the speech engine's voice label.
MAX_SPEAKER_LENGTH = 120
_VOICE_LABEL = re.compile(r"\s*\(spk_\d+\)$")

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


def speaker_link(
    value: Any,
    participant_name: Any,
    source: Any,
    linked_at: str,
    linked_by: str,
) -> Tuple[Optional[Dict[str, Any]], str]:
    """``(link, "")`` to store on a reply, or ``(None, why not)``.

    ``value`` is what the capture client sent, by its ``Basis``
    (``same_segment`` when it gives none):

    * ``same_segment``: the ``SegmentId``, ``Channel`` and ``Speaker`` of the
      transcript line the reply was read from, as it wrote them. Only a reply
      said aloud (``source`` ``voice``) is one of the meeting's transcript
      lines, so only it can be linked this way. The line's speaker, without the
      speech engine's voice label, must be the reply's participant name
      (compared as replies are, by :func:`normalize_name`): the two come from
      one attribution, so a difference means the client named the reply and the
      line differently, and then neither is taken as the other.
    * ``platform_identity``: the ``PlatformParticipantId`` the meeting platform
      gave the chat reply's sender, and the ``Channel`` and ``Speaker`` of the
      transcript speaker most recently written with that id. Only a chat reply
      (``source`` ``chat``). Names are not compared: the id is the link.

    The link names the speaker; whether the transcript still confirms it is
    decided by whoever reads the record (lma_speakers.participants).
    """
    if not isinstance(value, Mapping):
        return None, "no link was given"
    basis = value.get("Basis") or SPEAKER_LINK_SAME_SEGMENT
    if basis not in SPEAKER_LINK_BASES:
        return None, f"Basis must be one of {', '.join(SPEAKER_LINK_BASES)}"
    channel = clip(value.get("Channel"), 32) or ""
    channel = _STORED_SPEECH_CHANNEL.get(channel, channel)
    if channel not in SPEAKER_LINK_CHANNELS:
        return None, f"Channel must be one of {', '.join(SPEAKER_LINK_CHANNELS)}"
    speaker = clip(value.get("Speaker"), MAX_SPEAKER_LENGTH + 1)
    if not speaker or len(speaker) > MAX_SPEAKER_LENGTH:
        return None, f"Speaker must be 1 to {MAX_SPEAKER_LENGTH} characters"
    if basis == SPEAKER_LINK_PLATFORM_IDENTITY:
        return _platform_identity_link(value, source, channel, speaker, linked_at, linked_by)
    if source != "voice":
        return None, "only a reply said aloud is a line of the transcript"
    segment_id = clip(value.get("SegmentId"), MAX_SEGMENT_ID_LENGTH + 1)
    if not segment_id or len(segment_id) > MAX_SEGMENT_ID_LENGTH:
        return None, f"SegmentId must be 1 to {MAX_SEGMENT_ID_LENGTH} characters"
    base = _VOICE_LABEL.sub("", speaker)[:MAX_NAME_LENGTH]
    name = normalize_name(participant_name)
    if not name or normalize_name(base) != name:
        return None, "the line's speaker is not the reply's participant"
    return {
        "Basis": SPEAKER_LINK_SAME_SEGMENT,
        "SegmentId": segment_id,
        "Channel": channel,
        "Speaker": speaker,
        "LinkedAt": linked_at,
        "LinkedBy": linked_by,
    }, ""


def _platform_identity_link(
    value: Mapping[str, Any],
    source: Any,
    channel: str,
    speaker: str,
    linked_at: str,
    linked_by: str,
) -> Tuple[Optional[Dict[str, Any]], str]:
    """A ``platform_identity`` link (see :func:`speaker_link`), or why not."""
    if source != "chat":
        return None, "only a chat reply is linked by its sender's platform id"
    platform_id = clip(value.get("PlatformParticipantId"), MAX_PLATFORM_ID_LENGTH + 1)
    if not platform_id or len(platform_id) > MAX_PLATFORM_ID_LENGTH:
        return None, f"PlatformParticipantId must be 1 to {MAX_PLATFORM_ID_LENGTH} characters"
    return {
        "Basis": SPEAKER_LINK_PLATFORM_IDENTITY,
        "PlatformParticipantId": platform_id,
        "Channel": channel,
        "Speaker": speaker,
        "LinkedAt": linked_at,
        "LinkedBy": linked_by,
    }, ""


def without_platform_ids(record: Optional[Mapping[str, Any]]) -> Tuple[Dict[str, Any], bool]:
    """The record with no reply's link holding a platform participant id, and whether one did.

    Used when a meeting's transcript is deleted: the segments that carried the
    ids are gone, and the consent record keeps no copy. A link keeps its basis
    and when it was made; it names no speaker once the transcript is gone.
    """
    out = dict(record or {})
    changed = False
    events = []
    for event in out.get("Events") or []:
        link = event.get("SpeakerLink") if isinstance(event, Mapping) else None
        if isinstance(link, Mapping) and "PlatformParticipantId" in link:
            kept = {key: item for key, item in link.items() if key != "PlatformParticipantId"}
            event = {**event, "SpeakerLink": kept}
            changed = True
        events.append(event)
    if changed:
        out["Events"] = events
    return out, changed


def _link_key(event: Mapping[str, Any]) -> Optional[Tuple[str, str]]:
    """The speaker a reply is linked to, as its channel and speaker as stored."""
    link = event.get("SpeakerLink")
    if not isinstance(link, Mapping) or not link.get("Basis"):
        return None
    return (str(link.get("Channel") or ""), str(link.get("Speaker") or "").strip())


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


def consented_outside_chat(record: Optional[Mapping[str, Any]]) -> List[str]:
    """The participants listed as present whose latest reply is a consent not given in the chat.

    A consent said aloud (``voice``) or entered in LMA (``ui``) is not in the
    meeting chat, so a check of a chat screenshot does not expect to see it.
    Participants with no reply, or whose latest reply is an objection or a
    consent typed in the chat, are not listed. Names as the record lists them.
    """
    if not record:
        return []
    latest: Dict[str, Tuple[str, Any]] = {}
    for event in record.get("Events") or []:
        if event.get("Source") == SOURCE_ATTESTATION:
            continue
        name = normalize_name(event.get("ParticipantName"))
        decision = event.get("Decision")
        if name and decision in DECISIONS:
            latest[name] = (decision, event.get("Source"))
    listed: List[str] = []
    for name in record.get("Participants") or []:
        decision, source = latest.get(normalize_name(name), (None, None))
        if decision == DECISION_CONSENT and source != "chat" and name not in listed:
            listed.append(name)
    return listed


def _deciding_positions(events: Sequence[Mapping[str, Any]]) -> set:
    """Each participant's latest reply, each linked speaker's latest reply, and
    the latest attestation entry.

    A linked speaker's latest reply is what the meeting export gives as that
    speaker's consent, so it is kept although a later reply under the same name
    is the one the status counts.
    """
    keep, seen = set(), set()
    for position in range(len(events) - 1, -1, -1):
        event = events[position]
        keys: List[Tuple[str, ...]] = []
        if event.get("Source") == SOURCE_ATTESTATION:
            keys.append(("attestation",))
        else:
            keys.append(("reply", normalize_name(event.get("ParticipantName"))))
            link = _link_key(event)
            if link is not None:
                keys.append(("speaker",) + link)
        for key in keys:
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
#: The event's id stays too: get_meeting_consent finds a platform_identity
#: link's stored id by it.
_ESSENTIAL_EVENT_FIELDS = ("EventId", "ParticipantName", "Decision", "Source", "OccurredAt")
#: What a speaker link keeps then: the line it names, how and when it was made.
_ESSENTIAL_LINK_FIELDS = (
    "Basis",
    "SegmentId",
    "PlatformParticipantId",
    "Channel",
    "Speaker",
    "LinkedAt",
)


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
    6. the replies the evidence check read in each image, and the participants it
       found no consent from, keeping its verdict;
    7. every reply's other fields (who recorded it and when), each
       evidence image's note and check summary, and the speaker link of every
       reply that is neither a participant's nor a linked speaker's latest;
    8. the remaining speaker links, oldest first. The status never depends on
       one; a reply without its link is no longer given as its speaker's in
       the meeting export.

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
        if isinstance(verification, dict):
            verification["Replies"] = []
            verification["MissingParticipants"] = []
            verification["ConsentedOutsideChat"] = []
    if record_size(record) <= limit:
        return record
    # 7. The replies kept, down to who, how, where and when, and the speaker
    #    link of each reply the export's per-speaker consent is read from.
    linked = _deciding_positions(record["Events"])
    shrunk = []
    for position, event in enumerate(record["Events"]):
        entry = {key: event.get(key) for key in _ESSENTIAL_EVENT_FIELDS}
        link = event.get("SpeakerLink")
        if position in linked and isinstance(link, Mapping) and link.get("Basis"):
            entry["SpeakerLink"] = {
                key: link.get(key) for key in _ESSENTIAL_LINK_FIELDS if key in link
            }
        shrunk.append(entry)
    record["Events"] = shrunk
    for entry in record.get("Evidence") or []:
        entry["Note"] = None
        if isinstance(entry.get("Verification"), dict):
            entry["Verification"]["Summary"] = None
    if record_size(record) <= limit:
        return record
    # 8. The speaker links, oldest first. The status never depends on one: a
    #    reply that loses its link is only no longer given as its speaker's.
    size = record_size(record)
    for event in record["Events"]:
        if size <= limit:
            break
        if event.get("SpeakerLink") is not None:
            size -= piece(event["SpeakerLink"]) + len('"SpeakerLink":,')
            event.pop("SpeakerLink")
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
