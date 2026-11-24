#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""A meeting as one structured record: the meeting export, schema version 1.

Published as docs/schemas/meeting-export-v1.schema.json and described in
docs/meeting-export.md. Within schema version 1 a field's name and meaning never
change; a new field is optional, and a consumer ignores what it does not know.

Built from what the archive function reads anyway -- the meeting row, its
transcript segments (``trs#``), key frames (``kf#``) and notifications
(``nt#``) and whiteboards (``wb#``) -- by :func:`build_export`, which reads nothing itself. Who may have
it is decided before it is built: the ``getCall`` rule, as for every archive.

What the export never holds: an image (key frames are given by their alt text),
a recording, a signed link, or the consent evidence (only whether it exists).
Once the transcript was deleted under the Retention settings, the transcript,
the links from the chat and the participants are left out, the checklist's
evidence has no quotes, and the whiteboards (deleted with the transcript) are an
empty list.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

# pylint: disable=import-error
from lma_channels import canonical_channel  # configuration layer
from lma_chat_links import extract_links  # configuration layer
from lma_consent.summary import (  # configuration layer
    consent_events,
    has_speaker_links,  # noqa: F401 -- index.py reads it through this module
    meeting_consent_output,
    participant_consent,
)
from lma_speakers.participants import Speakers, final_segments  # configuration layer
from lma_speakers.participants import segment_kind as _kind  # configuration layer
from lma_summary_profiles import DEFAULT_PROFILE, action_items_output  # configuration layer
from lma_whiteboards import rules as whiteboard_rules  # configuration layer

# pylint: enable=import-error

SCHEMA_VERSION = 1
#: What every export says it is, first, so a file is recognised by its content.
FORMAT = "lma-meeting-export"

#: The parts ``include`` names; every one by default.
PARTS = (
    "transcript",
    "chat",
    "summary",
    "checklist",
    "consent",
    "actionItems",
    "keyframes",
    "guidance",
    "whiteboards",
)
DEFAULT_INCLUDE = PARTS

CAMERA_CONTENT = "CAMERA"
GUIDANCE = "guidance"

MAX_ALT_TEXT = 200
KEYFRAME_IMAGE = re.compile(r"!\[([^\]\n]*)\]\(keyframe:[A-Za-z0-9_-]{1,64}\)")


def parse_include(value: Any) -> List[str]:
    """``include`` as a list of parts; the default when absent. ValueError for an unknown one."""
    if value in (None, "", []):
        return list(DEFAULT_INCLUDE)
    if isinstance(value, str):
        value = [part.strip() for part in value.split(",")]
    if not isinstance(value, (list, tuple)) or not all(isinstance(p, str) for p in value):
        raise ValueError(f"include must be a list of: {', '.join(PARTS)}")
    wanted: List[str] = []
    for part in value:
        name = part.strip()
        if name not in PARTS:
            raise ValueError(f"unknown part {part!r}; include takes: {', '.join(PARTS)}")
        if name not in wanted:
            wanted.append(name)
    return wanted


def _seconds(value: Any) -> Optional[float]:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _clock(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def _entries(value: Any) -> List[str]:
    """The entries of a SharedWith value, in every shape it is stored in."""
    if isinstance(value, str):
        parts = value.replace("[", ",").replace("]", ",").split(",")
    elif isinstance(value, (list, tuple, set, frozenset)):
        parts = [p for p in value if isinstance(p, str) and not any(c in p for c in "[],")]
    else:
        return []
    return [p.strip() for p in parts if p.strip()]


# ── the meeting ─────────────────────────────────────────────────────────────


def meeting_part(meeting: Mapping[str, Any], name: str) -> Dict[str, Any]:
    """``meeting``: what it is, who has it, when it ran, and how it is labelled."""
    ended = meeting.get("Status") == "ENDED"
    duration = _seconds(meeting.get("TotalConversationDurationMillis"))
    labels = {
        str(label.get("Key")): str(label.get("Value") or "")
        for label in meeting.get("Labels") or []
        if isinstance(label, Mapping) and label.get("Key")
    }
    part = {
        "meetingId": meeting.get("CallId"),
        "name": name,
        "platform": meeting.get("MeetingPlatform") or None,
        "captureSource": meeting.get("CaptureSource") or None,
        "owner": meeting.get("Owner") or None,
        "sharedWith": _entries(meeting.get("SharedWith")),
        "status": meeting.get("Status") or None,
        "startTime": meeting.get("CreatedAt") or None,
        # When an ended meeting last changed: its end, unless it was changed afterwards.
        "endTime": (meeting.get("UpdatedAt") or None) if ended else None,
        "durationSeconds": round(duration / 1000, 3) if duration is not None else None,
        "labels": labels,
        "audience": meeting.get("Audience") or None,
        "summaryProfile": meeting.get("SummaryProfile") or DEFAULT_PROFILE,
        "transcriptRetained": not meeting.get("TranscriptDeletedAt"),
    }
    if meeting.get("TranscriptDeletedAt"):
        part["transcriptDeletedAt"] = meeting["TranscriptDeletedAt"]
    return part


# ── the timeline ────────────────────────────────────────────────────────────


SegmentTimes = Dict[str, Tuple[Optional[float], Optional[float]]]


def segment_times(rows: Iterable[Mapping[str, Any]]) -> SegmentTimes:
    """Each final segment's (startTime, endTime), by its segment id."""
    times: SegmentTimes = {}
    for row in rows:
        segment_id = row.get("SegmentId")
        if segment_id is None or str(segment_id) in times:
            continue
        start = _seconds(row.get("StartTime"))
        end = _seconds(row.get("EndTime"))
        times[str(segment_id)] = (start, end if end is not None else start)
    return times


def with_times(reference: Dict[str, Any], start: Any, times: SegmentTimes) -> Dict[str, Any]:
    """``reference`` (which holds a ``segmentId``) with its ``startTime`` and ``endTime``.

    The start is the one stored with the reference, else its segment's; the end
    is its segment's. Either is null when the reference has no segment id, or
    names a segment the export was not given (the transcript was deleted, or
    the segment was not found), so a consumer that renumbers segments can still
    place it in time wherever LMA knows the time.
    """
    segment = times.get(str(reference.get("segmentId"))) if reference.get("segmentId") else None
    stored = _seconds(start)
    reference["startTime"] = stored if stored is not None else (segment[0] if segment else None)
    reference["endTime"] = segment[1] if segment else None
    return reference


def shared_frames(keyframes: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """The key frames of shared content (not the camera, not a repeat), in time order."""
    frames = []
    for item in keyframes:
        if not isinstance(item, Mapping):
            continue
        frame_id = str(item.get("FrameId") or str(item.get("SK") or "")[2:])
        if not frame_id or item.get("ContentKind") == CAMERA_CONTENT or item.get("DuplicateOf"):
            continue
        frames.append({**item, "FrameId": frame_id})
    return sorted(frames, key=lambda f: _seconds(f.get("Timestamp")) or 0.0)


def alt_text(frame: Mapping[str, Any]) -> str:
    """A key frame in words: its title, else the start of its text, else when it was shown."""
    for value in (frame.get("Title"), frame.get("SlideText")):
        text = " ".join(str(value or "").split())
        if text:
            return text if len(text) <= MAX_ALT_TEXT else text[: MAX_ALT_TEXT - 1].rstrip() + "…"
    return f"Shared screen at {_clock(_seconds(frame.get('Timestamp')) or 0.0)}"


def transcript_part(
    rows: Sequence[Mapping[str, Any]],
    speakers: Speakers,
    frames: Sequence[Mapping[str, Any]],
    with_speech: bool,
    with_chat: bool,
    with_screen: bool,
) -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []
    for row in rows:
        kind = _kind(row)
        if (kind == "speech" and not with_speech) or (kind == "chat" and not with_chat):
            continue
        start = _seconds(row.get("StartTime")) or 0.0
        entries.append(
            {
                "segmentId": str(row.get("SegmentId") or ""),
                "startTime": start,
                "endTime": _seconds(row.get("EndTime"))
                if row.get("EndTime") is not None
                else start,
                "speakerId": speakers.id_of(row),
                "channel": canonical_channel(row.get("Channel")) or row.get("Channel"),
                "kind": kind,
                "text": str(row.get("Transcript") or ""),
            }
        )
    if with_screen:
        for position, frame in enumerate(frames):
            text = " ".join(str(frame.get("SlideText") or "").split())
            if not text:
                continue
            start = _seconds(frame.get("Timestamp")) or 0.0
            following = frames[position + 1] if position + 1 < len(frames) else None
            entries.append(
                {
                    "segmentId": f"keyframe-{frame['FrameId']}",
                    "startTime": start,
                    "endTime": _seconds(following.get("Timestamp")) if following else start,
                    "speakerId": None,
                    "channel": None,
                    "kind": "screen",
                    "text": text,
                }
            )
    entries.sort(key=lambda e: e["startTime"])
    return entries


def links_part(rows: Sequence[Mapping[str, Any]], speakers: Speakers) -> List[Dict[str, Any]]:
    """Every address typed in the meeting chat, once, with who shared it first and when."""
    found, seen = [], set()
    for row in rows:
        if _kind(row) != "chat":
            continue
        for url in extract_links(row.get("Transcript")):
            if url in seen:
                continue
            seen.add(url)
            found.append(
                {
                    "url": url,
                    "startTime": _seconds(row.get("StartTime")) or 0.0,
                    "sharedBy": speakers.id_of(row),
                }
            )
    return found


# ── the rest ────────────────────────────────────────────────────────────────


def section_label(key: str) -> str:
    """A section key as a heading: ``ACTION ITEMS`` -> ``Action items``."""
    text = " ".join(key.split())
    return text[:1].upper() + text[1:].lower() if text.isupper() else text


def summary_part(
    meeting: Mapping[str, Any], sections: Sequence[Tuple[str, str]], state: Mapping[str, Any]
) -> Optional[Dict[str, Any]]:
    if not sections:
        return None
    return {
        "profile": state.get("profile") or DEFAULT_PROFILE,
        "source": state.get("source") or "generated",
        "sections": [
            {
                "key": key,
                "label": section_label(key),
                "markdown": KEYFRAME_IMAGE.sub(lambda m: f"({m.group(1)})", markdown or ""),
            }
            for key, markdown in sections
        ],
    }


def checklist_part(
    meeting: Mapping[str, Any], quotes: bool, times: Optional[SegmentTimes] = None
) -> List[Dict[str, Any]]:
    """The checklist as get_meeting_checklist gives its items, each piece of
    evidence with its segment's end time too; quotes only while the transcript is kept."""
    items = []
    for item in meeting.get("Checklist") or []:
        if not isinstance(item, Mapping):
            continue
        items.append(
            {
                "itemId": item.get("ItemId"),
                "text": item.get("Text"),
                "kind": item.get("Kind"),
                "priority": item.get("Priority") or "should",
                "source": item.get("Source"),
                "status": item.get("Status") or "open",
                "statusSource": item.get("StatusSource"),
                "evidence": [
                    {
                        **with_times(
                            {"segmentId": entry.get("SegmentId")},
                            entry.get("StartTime"),
                            times or {},
                        ),
                        "quote": entry.get("Quote") if quotes else None,
                    }
                    for entry in item.get("Evidence") or []
                    if isinstance(entry, Mapping)
                ],
                "note": item.get("Note"),
                "updatedBy": item.get("UpdatedBy"),
                "updatedAt": item.get("UpdatedAt"),
            }
        )
    return items


def guidance_part(notifications: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """The guidance given on the meeting (notifications of category guidance), oldest first."""
    items = [
        n
        for n in notifications
        if isinstance(n, Mapping)
        and str(n.get("SK") or "n#").startswith("n#")
        and n.get("Category") == GUIDANCE
    ]
    items.sort(key=lambda n: str(n.get("CreatedAt") or ""))
    return [
        {
            "type": n.get("Type"),
            "title": n.get("Title"),
            "body": n.get("Body"),
            "refs": [str(r) for r in n.get("Refs") or []],
            "createdAt": n.get("CreatedAt"),
        }
        for n in items
    ]


def whiteboards_part(rows: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """The meeting's whiteboards as they are now (no earlier versions), in the
    order they were made: the rows under ``wb#`` whose sort key is ``b#``."""
    boards = [
        r for r in rows if isinstance(r, Mapping) and str(r.get("SK") or "b#").startswith("b#")
    ]
    return [
        whiteboard_rules.whiteboard_output(b) for b in whiteboard_rules.sort_whiteboards(boards)
    ]


def build_export(
    meeting: Mapping[str, Any],
    *,
    segments: Sequence[Mapping[str, Any]] = (),
    keyframes: Sequence[Mapping[str, Any]] = (),
    notifications: Sequence[Mapping[str, Any]] = (),
    whiteboards: Sequence[Mapping[str, Any]] = (),
    include: Sequence[str] = DEFAULT_INCLUDE,
    sections: Sequence[Tuple[str, str]] = (),
    state: Optional[Mapping[str, Any]] = None,
    name: Optional[str] = None,
    lma_version: str = "unknown",
    now: Optional[Callable[[], dt.datetime]] = None,
) -> Dict[str, Any]:
    """The meeting export of one meeting, holding the parts ``include`` names."""
    wanted = set(include)
    moment = (now or (lambda: dt.datetime.now(dt.timezone.utc)))()
    retained = not meeting.get("TranscriptDeletedAt")
    call_id = str(meeting.get("CallId") or "")
    export: Dict[str, Any] = {
        "format": FORMAT,
        "schemaVersion": SCHEMA_VERSION,
        "exportedAt": moment.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "lmaVersion": lma_version or "unknown",
        "meeting": meeting_part(meeting, name or call_id),
    }
    agenda = meeting.get("Agenda")
    export["agenda"] = (
        {"text": agenda, "source": meeting.get("AgendaSource") or None} if agenda else None
    )
    export["context"] = meeting.get("MeetingContext") or None

    frames = shared_frames(keyframes)
    rows = final_segments(segments) if retained else []
    times = segment_times(rows)
    # Indexed over the whole transcript, so a speaker has the same id whichever
    # parts are asked for, and a consent event's speakerId names the same
    # participant as get_meeting_consent's does.
    speakers = Speakers(rows, meeting) if retained else None
    if speakers is not None and wanted & {"transcript", "chat"}:
        export["transcript"] = transcript_part(
            rows,
            speakers,
            frames,
            with_speech="transcript" in wanted,
            with_chat="chat" in wanted,
            with_screen="transcript" in wanted and "keyframes" in wanted,
        )
        if "chat" in wanted:
            export["links"] = links_part(rows, speakers)
        # Before the list: a speaker a consent event is linked to is listed.
        events = consent_events(meeting.get("Consent") or {}, speakers)
        export["participants"] = [
            {**entry, "consent": participant_consent(entry["speakerId"], events)}
            for entry in speakers.participants()
        ]
    if "consent" in wanted:
        # The same shape as get_meeting_consent's answer.
        export["consent"] = meeting_consent_output(
            call_id, {"Consent": meeting.get("Consent")}, speakers
        )
    if "checklist" in wanted:
        export["checklist"] = checklist_part(meeting, quotes=retained, times=times)
    if "keyframes" in wanted:
        export["keyframes"] = [
            {
                "keyframeId": frame["FrameId"],
                "startTime": _seconds(frame.get("Timestamp")) or 0.0,
                "altText": alt_text(frame),
            }
            for frame in frames
        ]
    if "summary" in wanted:
        summary = summary_part(meeting, sections, state or {})
        if summary is not None:
            export["summary"] = summary
    if "actionItems" in wanted:
        export["actionItems"] = [
            with_times(item, item.get("startTime"), times)
            for item in action_items_output(meeting.get("ActionItems"))
        ]
    if "guidance" in wanted:
        export["guidance"] = guidance_part(notifications)
    if "whiteboards" in wanted:
        # Drawn from the transcript, so deleted with it.
        export["whiteboards"] = whiteboards_part(whiteboards) if retained else []
    return export
