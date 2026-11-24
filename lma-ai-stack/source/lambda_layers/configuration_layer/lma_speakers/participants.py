# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""A meeting's transcript speakers as the meeting export's participants.

Each speaker of the transcript, with the id the export gives it
(``participants[].speakerId``). The export (meeting_archive) and the MCP
``get_meeting_consent`` tool both build the ids here, from the same final
segments, so a consent event's ``speakerId`` names the same participant in
both. See docs/meeting-export.md.

Speech is grouped by its channel and its speaker as stored (one voice); a chat
message by its sender. The id is opaque: ``remote-spk_0`` for a separated
voice, ``local-1`` for a channel's unseparated speaker, ``chat-1`` for a chat
sender. Every final row is given its id when the index is built, in time
order, so an id does not depend on which parts of the export were asked for.
The name is a person's name only where one was given or confirmed; otherwise
null, never a guess.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from lma_channels import canonical_channel

from . import (
    DEFAULT_OTHER_SPEAKER_NAME,
    is_unknown_name,
    looks_like_room,
    parse_speaker_identities,
    resolve_merge_target,
    resolve_speaker,
    speaker_groups,
    speaker_key,
    split_diarization_label,
)

MEETING_CHAT = "MEETING_CHAT"
ASSISTANT_CHANNELS = ("AGENT_ASSISTANT", "CHAT_ASSISTANT")

#: Where a participant's name came from (``nameSource``). ``enrolled`` is part
#: of the format but not produced: an enrolled voice is stored as its name alone,
#: the same form a platform's name takes, so a stored label cannot show it.
NAME_SOURCES = ("enrolled", "manual", "transcript", "platform", "none")
#: The speaker-naming bases that name somebody from what was said
#: (speaker_name_resolver: a self-introduction, or being addressed by name).
TRANSCRIPT_BASES = ("self-introduction", "addressed-by-name")

VOICE_ID = re.compile(r"^spk_\d+$")
#: The SpeakerLink basis of a chat reply linked by its sender's platform id
#: (lma_consent.SPEAKER_LINK_PLATFORM_IDENTITY).
PLATFORM_IDENTITY = "platform_identity"

#: Every segment field that final_segments and Speakers read, so a reader that
#: only needs speaker ids (get_meeting_consent) can read just these from the
#: transcript; test_lma_speakers holds the two to this list.
SPEAKER_FIELDS = (
    "PK",
    "SegmentId",
    "StartTime",
    "EndTime",
    "IsPartial",
    "Channel",
    "Speaker",
    # A platform_identity link is confirmed by the id its speaker's rows carry.
    "PlatformParticipantId",
)


def segment_kind(row: Mapping[str, Any]) -> str:
    """``speech``, ``chat`` or ``assistant``: what a segment's channel holds."""
    channel = row.get("Channel")
    if channel == MEETING_CHAT:
        return "chat"
    if channel in ASSISTANT_CHANNELS:
        return "assistant"
    return "speech"


def _seconds(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def final_segments(segments: Iterable[Mapping[str, Any]]) -> List[Mapping[str, Any]]:
    """The meeting's final segments of speech and chat, in time order."""
    rows = [
        s
        for s in segments
        if isinstance(s, Mapping)
        and s.get("IsPartial") is not True
        and segment_kind(s) != "assistant"
        and str(s.get("PK") or "trs#").startswith("trs#")
    ]
    return sorted(
        rows, key=lambda s: (_seconds(s.get("StartTime")) or 0.0, str(s.get("SegmentId")))
    )


class Speakers:
    """Each speaker of the transcript as a participant, with an id unique in the export."""

    def __init__(self, segments: Sequence[Mapping[str, Any]], meeting: Mapping[str, Any]):
        self.identities = parse_speaker_identities(meeting.get("SpeakerIdentities"))
        self.speech = [s for s in segments if segment_kind(s) == "speech"]
        self.groups = speaker_groups(self.speech, self.identities)
        self.fallback = meeting.get("CustomerPhoneNumber")
        self.by_key: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self.order: List[Dict[str, Any]] = []
        self.used: Set[str] = set()
        self.rows_by_id: Dict[str, Mapping[str, Any]] = {}
        # Each voice's final rows of speech, by channel and speaker as stored.
        self.rows_by_key: Dict[Tuple[str, str], List[Mapping[str, Any]]] = {}
        self._numbers: Dict[str, int] = {}
        # Every row is given its id now, in time order, so the ids are the same
        # whichever rows a reader goes on to ask about.
        for row in segments:
            if segment_kind(row) == "assistant":
                continue
            self._entry(row)
            if segment_kind(row) == "speech":
                self.rows_by_key.setdefault(self._key(row), []).append(row)
            segment_id = row.get("SegmentId")
            if segment_id is not None:
                self.rows_by_id.setdefault(str(segment_id), row)

    def _next(self, prefix: str) -> str:
        self._numbers[prefix] = self._numbers.get(prefix, 0) + 1
        return f"{prefix}-{self._numbers[prefix]}"

    @staticmethod
    def _key(row: Mapping[str, Any]) -> Tuple[str, str]:
        stored = str(row.get("Speaker") or "").strip()
        channel = canonical_channel(row.get("Channel")) or str(row.get("Channel") or "")
        return (MEETING_CHAT if segment_kind(row) == "chat" else channel, stored)

    def _entry(self, row: Mapping[str, Any]) -> Dict[str, Any]:
        key = self._key(row)
        known = self.by_key.get(key)
        if known is not None:
            return known
        stored = key[1]
        channel = key[0]
        entry: Dict[str, Any]
        if segment_kind(row) == "chat":
            entry = {
                "speakerId": self._next("chat"),
                "label": stored or "Unknown sender",
                "name": stored if stored and not is_unknown_name(stored) else None,
                "nameSource": "platform" if stored and not is_unknown_name(stored) else "none",
                "email": None,
            }
        else:
            base, suffix = split_diarization_label(stored)
            voice = suffix.strip()[1:-1] if suffix else ""
            prefix = channel.lower() or "speech"
            speaker_id = f"{prefix}-{voice}" if VOICE_ID.match(voice) else self._next(prefix)
            while any(e["speakerId"] == speaker_id for e in self.order):
                speaker_id = self._next(prefix)
            name, source, label = self._name(row, base.strip())
            entry = {
                "speakerId": speaker_id,
                "label": label,
                "name": name,
                "nameSource": source,
                "email": None,
            }
        self.by_key[key] = entry
        self.order.append(entry)
        return entry

    def id_of(self, row: Mapping[str, Any]) -> str:
        """The id of the row's speaker, which the export then lists as a participant."""
        entry = self._entry(row)
        self.used.add(str(entry["speakerId"]))
        return str(entry["speakerId"])

    def _name(self, row: Mapping[str, Any], base: str) -> Tuple[Optional[str], str, str]:
        """(name, nameSource, label) of one voice, from the meeting's speaker identities."""
        resolved = resolve_speaker(row, self.identities, self.fallback, self.groups)
        label = str(resolved.get("name") or base or "Unknown speaker")
        if resolved.get("overridden"):
            # A name given to one line alone does not name the voice.
            resolved = resolve_speaker(
                {**row, "SegmentId": None}, self.identities, self.fallback, self.groups
            )
            label = str(resolved.get("name") or base or "Unknown speaker")
        status = resolved.get("status")
        if status == "confirmed":
            target = resolve_merge_target(
                speaker_key(row.get("Channel"), row.get("Speaker")), self.identities["merges"]
            )
            entry = self.identities["identities"].get(target)
            source = entry.get("source") if isinstance(entry, Mapping) else None
            kind = "transcript" if source in TRANSCRIPT_BASES else "manual"
            return str(resolved["name"]), kind, label
        if status == "proposed":
            # Suggested from the transcript and not confirmed by anybody: not a name yet.
            return None, "none", base or label
        if base and base != DEFAULT_OTHER_SPEAKER_NAME and not is_unknown_name(base):
            if not looks_like_room(base):
                return base, "platform", label
        return None, "none", label

    def linked_id(self, link: Any) -> Optional[str]:
        """The id of the speaker a consent event's ``SpeakerLink`` names, when the transcript confirms it.

        A ``same_segment`` link is confirmed only when the transcript still
        holds the linked segment as a final line of speech, on the linked
        channel, under exactly the linked speaker as stored, and nobody has
        named that one line as somebody else's on the meeting page. A
        ``platform_identity`` link is confirmed by :meth:`_platform_linked_id`.
        The comparison is of stored values, never of names. A linked speaker is
        listed as a participant.
        """
        if not isinstance(link, Mapping):
            return None
        if link.get("Basis") == PLATFORM_IDENTITY:
            return self._platform_linked_id(link)
        segment_id = str(link.get("SegmentId") or "")
        row = self.rows_by_id.get(segment_id) if segment_id else None
        if row is None or segment_kind(row) != "speech":
            return None
        if (canonical_channel(row.get("Channel")) or "") != (
            canonical_channel(link.get("Channel")) or ""
        ):
            return None
        if str(row.get("Speaker") or "").strip() != str(link.get("Speaker") or "").strip():
            return None
        override = (self.identities.get("segmentOverrides") or {}).get(segment_id)
        if isinstance(override, str) and override:
            return None
        return self.id_of(row)

    def _platform_linked_id(self, link: Mapping[str, Any]) -> Optional[str]:
        """The speaker a ``platform_identity`` link names, when the transcript confirms it.

        Confirmed when the transcript holds final lines of speech on the linked
        channel, under exactly the linked speaker as stored, that carry the
        linked ``PlatformParticipantId``; when none of that speaker's lines
        carries another participant's id (one voice label over two people is
        nobody's); and when nobody has named one of those lines as somebody
        else's on the meeting page. Stored values are compared, never names.
        """
        platform_id = str(link.get("PlatformParticipantId") or "")
        if not platform_id:
            return None
        channel = canonical_channel(link.get("Channel")) or str(link.get("Channel") or "")
        rows = self.rows_by_key.get((channel, str(link.get("Speaker") or "").strip())) or []
        ids = {
            str(row.get("PlatformParticipantId"))
            for row in rows
            if row.get("PlatformParticipantId")
        }
        if ids != {platform_id}:
            return None
        carrying = [
            row for row in rows if str(row.get("PlatformParticipantId") or "") == platform_id
        ]
        overrides = self.identities.get("segmentOverrides") or {}
        for row in carrying:
            override = overrides.get(str(row.get("SegmentId") or ""))
            if isinstance(override, str) and override:
                return None
        return self.id_of(carrying[0])

    def participants(self) -> List[Dict[str, Any]]:
        """The speakers a reader asked about, in the order they first speak."""
        return [dict(entry) for entry in self.order if entry["speakerId"] in self.used]
