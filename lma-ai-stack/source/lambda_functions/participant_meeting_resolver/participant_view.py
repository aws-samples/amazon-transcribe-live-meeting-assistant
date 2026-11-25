#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""What a Participant is shown of a meeting shared with them: an allowlist.

Each function here builds its answer from the fields it names and nothing else,
so an attribute the meeting record holds, or gains later, is not shown unless
it is added here. docs/user-based-access-control.md#participant lists what is
shown and what is not; test_index.py holds the fields here to that list.

Shown: the meeting's name, times, status and length; who shared it (its
owner); the platform; the agenda; the summary, its state and its action items;
the speaker names (the name map, without the names a user rejected, the
pasted attendee candidates or the naming pass's bookkeeping); the names on the
participant list, without their addresses; the consent record's mode, status,
notice and replies; whether there are recordings, and the capture sessions
their playback is placed by; and when the transcript or the recordings were
deleted under the Retention settings.

Not shown, among everything else the record holds: the meeting context, the
checklist and its evidence, the labels, the audience, the summary profile, the
live guidance and whiteboard switches, the sharing list, the participant
addresses, the recording links themselves (getParticipantMeetingRecordings
signs its own), the consent record's attestation statement, evidence and
status history, the sentiment, the capture and transcription details, and
the metadata.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Mapping, Optional

try:
    from lma_channels import STORED_TRANSCRIPT_CHANNELS  # configuration layer
    from lma_speakers import parse_speaker_identities
except ImportError:  # pragma: no cover - the layer is attached to the function
    STORED_TRANSCRIPT_CHANNELS = ("REMOTE", "LOCAL", "CALLER", "AGENT", "MEETING_CHAT")
    parse_speaker_identities = None  # type: ignore[assignment]

#: The transcript channels a Participant reads: what was said and what was typed
#: in the meeting chat. The meeting assistant's answers (AGENT_ASSISTANT for the
#: wake phrase, CHAT_ASSISTANT for the meeting page's chat) are the owner's.
TRANSCRIPT_CHANNELS = tuple(STORED_TRANSCRIPT_CHANNELS)

#: The fields of each kind, as the GraphQL types declare them.
MEETING_SUMMARY_FIELDS = (
    "CallId",
    "CreatedAt",
    "UpdatedAt",
    "Status",
    "TotalConversationDurationMillis",
    "Owner",
    "MeetingPlatform",
)
MEETING_FIELDS = MEETING_SUMMARY_FIELDS + (
    "Agenda",
    "CallSummaryText",
    "SummaryStatus",
    "ActionItems",
    "SpeakerIdentities",
    "OtherParticipantsName",
    "ParticipantNames",
    "Consent",
    "HasAudioRecording",
    "HasVideoRecording",
    "Sessions",
    "TranscriptDeletedAt",
    "RecordingsDeletedAt",
)
TRANSCRIPT_SEGMENT_FIELDS = (
    "CallId",
    "SegmentId",
    "StartTime",
    "EndTime",
    "Transcript",
    "IsPartial",
    "Channel",
    "Speaker",
    "LanguageCode",
    "CreatedAt",
)
ACTION_ITEM_FIELDS = ("Text", "Owner", "DueDate", "DueText", "StartTime", "SegmentId")
CONSENT_FIELDS = (
    "Mode",
    "Rule",
    "Status",
    "CaptureAllowed",
    "NoticeText",
    "NoticePostedAt",
    "UpdatedAt",
)
CONSENT_REPLY_FIELDS = ("ParticipantName", "Decision", "Source", "OccurredAt")
SESSION_FIELDS = ("SessionNumber", "OffsetSeconds", "StartedAt")


def _pick(item: Mapping[str, Any], fields: Iterable[str]) -> Dict[str, Any]:
    return {field: item[field] for field in fields if item.get(field) is not None}


def meeting_summary(meeting: Mapping[str, Any]) -> Dict[str, Any]:
    """One row of a Participant's meetings list."""
    return _pick(meeting, MEETING_SUMMARY_FIELDS)


def _summary_status(raw: Any) -> Optional[str]:
    try:
        state = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return None
    status = state.get("status") if isinstance(state, Mapping) else None
    return status if isinstance(status, str) else None


def speaker_names(raw: Any) -> Optional[str]:
    """The speaker name map, as the page resolves the transcript's speakers with.

    Kept: the name given to each voice (and whether it is locked or confirmed),
    the merges of voices onto one person, the names given to single segments,
    and the one-person-or-several choice per name. Left out: the candidates
    (an attendee list pasted for the naming pass), the names a user rejected,
    and the naming pass's own bookkeeping.
    """
    if not raw or parse_speaker_identities is None:
        return None
    parsed = parse_speaker_identities(raw)
    identities = {}
    for key, entry in parsed["identities"].items():
        if isinstance(entry, Mapping) and isinstance(entry.get("name"), str) and entry["name"]:
            identities[key] = {
                name: entry[name]
                for name in ("name", "status", "locked")
                if isinstance(entry.get(name), (str, bool))
            }
    merges = {k: v for k, v in parsed["merges"].items() if isinstance(v, str)}
    overrides = {k: v for k, v in parsed["segmentOverrides"].items() if isinstance(v, str)}
    modes = {
        key: {"mode": value["mode"]}
        for key, value in parsed["speakerModes"].items()
        if isinstance(value, Mapping) and isinstance(value.get("mode"), str)
    }
    if not (identities or merges or overrides or modes):
        return None
    return json.dumps(
        {
            "version": 1,
            "identities": identities,
            "merges": merges,
            "segmentOverrides": overrides,
            "speakerModes": modes,
        }
    )


def consent_record(consent: Any) -> Optional[Dict[str, Any]]:
    """The meeting's consent record as a Participant reads it: mode, status, notice and replies."""
    if not isinstance(consent, Mapping):
        return None
    out = _pick(consent, CONSENT_FIELDS)
    events = consent.get("Events") if isinstance(consent.get("Events"), list) else []
    out["Replies"] = [
        _pick(event, CONSENT_REPLY_FIELDS) for event in events if isinstance(event, Mapping)
    ]
    return out


def recording_links(meeting: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Each stored recording link: {"kind": "AUDIO"|"VIDEO", "url", "session"}, each link once.

    The lists (RecordingUrls, VideoRecordingUrls) hold one link per capture
    session, with the session at the same index of RecordingUrlSessions and
    VideoRecordingUrlSessions; RecordingUrl and VideoRecordingUrl name the
    latest, and are all a meeting recorded before the lists has.
    """
    links: List[Dict[str, Any]] = []
    for kind, latest, every, sessions in (
        ("AUDIO", "RecordingUrl", "RecordingUrls", "RecordingUrlSessions"),
        ("VIDEO", "VideoRecordingUrl", "VideoRecordingUrls", "VideoRecordingUrlSessions"),
    ):
        urls = meeting.get(every) if isinstance(meeting.get(every), list) else []
        numbers = meeting.get(sessions) if isinstance(meeting.get(sessions), list) else []
        seen = set()
        for position, url in enumerate([*urls, meeting.get(latest)]):
            if not isinstance(url, str) or not url or url in seen:
                continue
            seen.add(url)
            session = (
                numbers[position] if position < len(urls) and position < len(numbers) else None
            )
            links.append({"kind": kind, "url": url, "session": session})
    return links


def participant_meeting(meeting: Mapping[str, Any]) -> Dict[str, Any]:
    """A meeting shared with a Participant, as ParticipantMeeting."""
    out = meeting_summary(meeting)
    if isinstance(meeting.get("Agenda"), str):
        out["Agenda"] = meeting["Agenda"]
    if isinstance(meeting.get("CallSummaryText"), str):
        out["CallSummaryText"] = meeting["CallSummaryText"]
    status = _summary_status(meeting.get("CallSummaryState"))
    if status:
        out["SummaryStatus"] = status
    items = meeting.get("ActionItems") if isinstance(meeting.get("ActionItems"), list) else []
    out["ActionItems"] = [
        _pick(item, ACTION_ITEM_FIELDS)
        for item in items
        if isinstance(item, Mapping) and item.get("Text")
    ]
    names = speaker_names(meeting.get("SpeakerIdentities"))
    if names:
        out["SpeakerIdentities"] = names
    if isinstance(meeting.get("CustomerPhoneNumber"), str):
        out["OtherParticipantsName"] = meeting["CustomerPhoneNumber"]
    listed = meeting.get("Participants") if isinstance(meeting.get("Participants"), list) else []
    out["ParticipantNames"] = [
        p["name"]
        for p in listed
        if isinstance(p, Mapping) and isinstance(p.get("name"), str) and p["name"]
    ]
    consent = consent_record(meeting.get("Consent"))
    if consent:
        out["Consent"] = consent
    links = [] if meeting.get("RecordingsDeletedAt") else recording_links(meeting)
    out["HasAudioRecording"] = any(link["kind"] == "AUDIO" for link in links)
    out["HasVideoRecording"] = any(link["kind"] == "VIDEO" for link in links)
    sessions = meeting.get("Sessions") if isinstance(meeting.get("Sessions"), list) else []
    out["Sessions"] = [_pick(s, SESSION_FIELDS) for s in sessions if isinstance(s, Mapping)]
    for field in ("TranscriptDeletedAt", "RecordingsDeletedAt"):
        if meeting.get(field):
            out[field] = meeting[field]
    return out


def transcript_segment(item: Mapping[str, Any]) -> Dict[str, Any]:
    """One transcript line as ParticipantTranscriptSegment."""
    return _pick(item, TRANSCRIPT_SEGMENT_FIELDS)
