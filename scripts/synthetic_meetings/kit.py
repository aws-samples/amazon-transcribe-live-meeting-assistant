# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""The pure half of the synthetic meeting kit: no AWS, no network.

A meeting script (``meetings/<name>.yaml``) names its speakers and lists what
happens in order: spoken turns and meeting chat messages. This module loads and
checks a script, lays it out on a meeting timeline, decides when each event is
delivered for a given pacing, builds the Kinesis records the call event
processor reads (START, ADD_TRANSCRIPT_SEGMENT, END), and assembles the stereo
audio the WebSocket transcriber is given. ``delivery.py`` does the AWS calls and
``run_meeting.py`` is the command line; both import this.

The timeline is the scripted meeting's own clock, in seconds from the start of
the meeting. A segment's ``StartTime`` and ``EndTime`` are always on it, whatever
the pacing: ``--pace fast`` changes when the records are sent, not what they say.
"""

from __future__ import annotations

import array
import io
import json
import math
import re
import sys
import wave
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

MEETINGS_DIR = Path(__file__).resolve().parent / "meetings"

#: The speech channels (lma_channels in the AI stack's configuration layer).
REMOTE = "REMOTE"
LOCAL = "LOCAL"
SPEECH_CHANNELS = (REMOTE, LOCAL)
#: The channel a meeting chat message is stored on.
CHAT_CHANNEL = "MEETING_CHAT"

#: Polly ``pcm`` output at this rate is 16-bit signed little-endian mono.
SAMPLE_RATE = 16000
SAMPLE_WIDTH = 2
#: Audio is sent to the transcriber in chunks of this length, as ws_stream_probe does.
CHUNK_MS = 200

#: Silence before the first turn and after the last, in the audio.
LEAD_IN_SECONDS = 1.0
TAIL_SECONDS = 1.5
#: Silence before a turn that sets no ``pause``.
DEFAULT_TURN_PAUSE = 0.7
#: How long after the previous turn ends a chat message that sets no ``pause`` is typed.
DEFAULT_CHAT_PAUSE = 1.0
#: Speaking rate used to estimate a turn's length when no audio is synthesized (the
#: Kinesis path). Polly's neural voices speak at roughly 150-170 words a minute.
WORDS_PER_SECOND = 2.6
#: Turns shorter than this are hard for speaker diarization to attribute.
MIN_TURN_SECONDS = 2.5
#: Amazon Polly accepts at most 3000 characters of plain text per request.
POLLY_TEXT_LIMIT = 1500

#: ``--pace fast`` on the audio path: the rate ws_stream_probe streams at. The
#: transcriber buffers what it is sent and forwards it to the speech engine, which
#: is the limit; faster rates were not measured.
AUDIO_FAST_SPEED = 2.0

#: The label every kit meeting carries, so its meetings can be found and cleaned up.
LABEL_KEY = "synthetic-meeting"

#: The START fields createCall accepts (CreateCallInput in
#: lma-ai-stack/source/appsync/schema.graphql). The call event processor passes
#: the START record to createCall whole, so another field fails the write.
START_FIELDS = frozenset(
    {
        "CallId",
        "AgentId",
        "CreatedAt",
        "CustomerPhoneNumber",
        "SystemPhoneNumber",
        "Metadatajson",
        "ExpiresAfter",
        "Owner",
        "SharedWith",
        "Agenda",
        "AgendaSource",
        "AgendaUpdatedBy",
        "AgendaUpdatedAt",
        "Labels",
        "Audience",
        "MeetingMetadataUpdatedAt",
        "MeetingContext",
        "MeetingContextUpdatedBy",
        "MeetingContextUpdatedAt",
        "Checklist",
        "ChecklistUpdatedAt",
        "SummaryProfile",
        "GuidanceEnabled",
        "WhiteboardsEnabled",
        "MeetingPlatform",
        "Participants",
        "ParticipantsUpdatedBy",
        "ParticipantsUpdatedAt",
        "CaptureSource",
        "TranscriptionEngine",
        "TranscriptionLanguage",
        "SpeakerSeparation",
    }
)
#: Fields of the record itself that the processor reads and strips.
EVENT_FIELDS = frozenset({"EventType", "AccessToken", "IdToken", "RefreshToken"})

#: Amazon Polly neural voices that speak English. Another voice is allowed (Polly
#: decides), but gets a validation warning, since a typo is the usual reason.
NEURAL_ENGLISH_VOICES = frozenset(
    {
        "Danielle",
        "Gregory",
        "Ivy",
        "Joanna",
        "Joey",
        "Justin",
        "Kendra",
        "Kevin",
        "Kimberly",
        "Matthew",
        "Ruth",
        "Salli",
        "Stephen",
        "Amy",
        "Arthur",
        "Brian",
        "Emma",
        "Olivia",
        "Kajal",
        "Aria",
        "Ayanda",
        "Niamh",
    }
)

#: Every partition a meeting's rows live under in the EventSourcingTable, as
#: meeting_controls_resolver's delete_meeting removes them: the meeting row (c#),
#: transcript segments (trs#), notifications (nt#), live guidance state (gd#),
#: metering (mtr#), whiteboards (wb#), key frames (kf#) and chat attachments (att#).
MEETING_PARTITION_PREFIXES = ("c#", "trs#", "nt#", "gd#", "mtr#", "wb#", "kf#", "att#")


class ScriptError(ValueError):
    """A meeting script that cannot be used, with the reason."""


@dataclass(frozen=True)
class Speaker:
    name: str
    channel: str
    voice: str


@dataclass(frozen=True)
class Item:
    """One scripted event: a spoken turn or a chat message, in script order."""

    kind: str  # "turn" or "chat"
    name: str  # the speaker, or the chat message's sender
    text: str
    pause: float


@dataclass(frozen=True)
class Meeting:
    name: str
    title: str
    purpose: str
    agenda: str
    meeting_context: str
    speakers: tuple[Speaker, ...]
    items: tuple[Item, ...]
    path: Path | None = None

    def speaker(self, name: str) -> Speaker:
        for speaker in self.speakers:
            if speaker.name == name:
                return speaker
        raise KeyError(name)

    @property
    def turns(self) -> list[Item]:
        return [item for item in self.items if item.kind == "turn"]

    @property
    def chats(self) -> list[Item]:
        return [item for item in self.items if item.kind == "chat"]


@dataclass(frozen=True)
class TimedItem:
    """An item placed on the meeting timeline."""

    index: int  # position in Meeting.items
    kind: str
    name: str
    channel: str
    text: str
    start: float
    end: float

    @property
    def send_at(self) -> float:
        """Meeting time at which it is delivered: a turn once spoken, a chat once typed."""
        return self.end

    @property
    def turn(self) -> bool:
        return self.kind == "turn"


@dataclass(frozen=True)
class StopAfter:
    """Where ``--stop-after`` leaves the meeting: after a meeting time, or after N turns."""

    seconds: float | None = None
    turns: int | None = None


# --- loading and checking scripts -------------------------------------------


def _text(value: Any, where: str, *, required: bool = True) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str) or (required and not value.strip()):
        raise ScriptError(f"{where} must be a non-empty string")
    return value.strip()


def _pause(value: Any, where: str, default: float) -> float:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ScriptError(f"{where}: pause must be a number of seconds, 0 or more")
    return float(value)


def parse_meeting(data: Any, name: str, path: Path | None = None) -> Meeting:
    """A Meeting from a script's parsed YAML/JSON, or ScriptError saying what is wrong."""
    if not isinstance(data, dict):
        raise ScriptError(f"{name}: a meeting script is a mapping")
    unknown = set(data) - {"title", "purpose", "agenda", "meeting_context", "speakers", "script"}
    if unknown:
        raise ScriptError(f"{name}: unknown top-level keys {sorted(unknown)}")
    title = _text(data.get("title"), f"{name}: title")
    if re.search(r"[/?#%+&]", title):
        raise ScriptError(f"{name}: title must not contain / ? # % + or & (the UI replaces them)")
    speakers: list[Speaker] = []
    raw_speakers = data.get("speakers")
    if not isinstance(raw_speakers, list) or not raw_speakers:
        raise ScriptError(f"{name}: speakers must be a non-empty list")
    for position, raw in enumerate(raw_speakers):
        where = f"{name}: speakers[{position}]"
        if not isinstance(raw, dict):
            raise ScriptError(f"{where} must be a mapping")
        channel = raw.get("channel")
        if channel not in SPEECH_CHANNELS:
            raise ScriptError(f"{where}: channel must be one of {SPEECH_CHANNELS}")
        speaker = Speaker(
            name=_text(raw.get("name"), f"{where}.name"),
            channel=channel,
            voice=_text(raw.get("voice"), f"{where}.voice"),
        )
        if any(s.name == speaker.name for s in speakers):
            raise ScriptError(f"{where}: speaker {speaker.name!r} is listed twice")
        speakers.append(speaker)
    names = {s.name for s in speakers}

    items: list[Item] = []
    raw_items = data.get("script")
    if not isinstance(raw_items, list) or not raw_items:
        raise ScriptError(f"{name}: script must be a non-empty list of turns and chat messages")
    for position, raw in enumerate(raw_items):
        where = f"{name}: script[{position}]"
        if not isinstance(raw, dict):
            raise ScriptError(f"{where} must be a mapping")
        unknown = set(raw) - {"speaker", "chat", "text", "pause"}
        if unknown:
            raise ScriptError(f"{where}: unknown keys {sorted(unknown)}")
        if ("speaker" in raw) == ("chat" in raw):
            raise ScriptError(f"{where}: give exactly one of speaker (a turn) or chat (a message)")
        text = " ".join(_text(raw.get("text"), f"{where}.text").split())
        if "speaker" in raw:
            speaker_name = raw["speaker"]
            if speaker_name not in names:
                raise ScriptError(f"{where}: speaker {speaker_name!r} is not in speakers")
            items.append(
                Item(
                    "turn", speaker_name, text, _pause(raw.get("pause"), where, DEFAULT_TURN_PAUSE)
                )
            )
        else:
            sender = _text(raw["chat"], f"{where}.chat")
            items.append(
                Item("chat", sender, text, _pause(raw.get("pause"), where, DEFAULT_CHAT_PAUSE))
            )
    if not any(item.kind == "turn" for item in items):
        raise ScriptError(f"{name}: a meeting needs at least one spoken turn")
    return Meeting(
        name=name,
        title=title,
        purpose=_text(data.get("purpose"), f"{name}: purpose", required=False),
        agenda=_text(data.get("agenda"), f"{name}: agenda", required=False),
        meeting_context=_text(
            data.get("meeting_context"), f"{name}: meeting_context", required=False
        ),
        speakers=tuple(speakers),
        items=tuple(items),
        path=path,
    )


def load_meeting(path: Path) -> Meeting:
    """Load a script from a .yaml, .yml or .json file."""
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".json":
        data = json.loads(text)
    else:
        import yaml  # pylint: disable=import-outside-toplevel

        data = yaml.safe_load(text)
    return parse_meeting(data, path.stem, path)


def bundled_meetings() -> list[Path]:
    return sorted(p for p in MEETINGS_DIR.iterdir() if p.suffix in (".yaml", ".yml", ".json"))


def find_meeting(name_or_path: str) -> Meeting:
    """A bundled meeting by name (``sprint_planning``), or a script by path."""
    candidate = Path(name_or_path).expanduser()
    if candidate.is_file():
        return load_meeting(candidate)
    for path in bundled_meetings():
        if path.stem == name_or_path:
            return load_meeting(path)
    known = ", ".join(p.stem for p in bundled_meetings())
    raise ScriptError(
        f"no meeting {name_or_path!r}: not a file, and not a bundled meeting ({known})"
    )


def estimate_seconds(text: str) -> float:
    """How long a turn takes to say, estimated from its word count."""
    return round(max(1.0, len(text.split()) / WORDS_PER_SECOND), 3)


def warnings(meeting: Meeting) -> list[str]:
    """What is legal but likely to disappoint: turns too short to diarize, unknown voices."""
    found = []
    for speaker in meeting.speakers:
        if speaker.voice not in NEURAL_ENGLISH_VOICES:
            found.append(
                f"speaker {speaker.name!r}: {speaker.voice!r} is not a known neural English voice"
            )
    for position, item in enumerate(meeting.items):
        if item.kind == "turn" and estimate_seconds(item.text) < MIN_TURN_SECONDS:
            found.append(
                f"script[{position}] ({item.name}): about {estimate_seconds(item.text)} s; turns under "
                f"{MIN_TURN_SECONDS} s are hard for diarization to attribute"
            )
    return found


# --- the timeline and pacing -------------------------------------------------


def build_timeline(
    meeting: Meeting, durations: Mapping[int, float] | None = None
) -> list[TimedItem]:
    """Place every item on the meeting clock.

    ``durations`` maps an item's index to its spoken length in seconds (the
    synthesized audio's, on the audio path); a turn without one is estimated from
    its words. Turns follow one another, each after its pause. A chat message is
    typed ``pause`` seconds after the previous turn ended and takes no time, so the
    next turn's start does not move. Returned in order of time.
    """
    cursor = LEAD_IN_SECONDS
    first_turn = True
    placed: list[TimedItem] = []
    for index, item in enumerate(meeting.items):
        if item.kind == "turn":
            length = (durations or {}).get(index) or estimate_seconds(item.text)
            start = cursor + (0.0 if first_turn else item.pause)
            first_turn = False
            end = start + length
            placed.append(
                TimedItem(
                    index,
                    "turn",
                    item.name,
                    meeting.speaker(item.name).channel,
                    item.text,
                    round(start, 3),
                    round(end, 3),
                )
            )
            cursor = end
        else:
            at = round(cursor + item.pause, 3)
            placed.append(TimedItem(index, "chat", item.name, CHAT_CHANNEL, item.text, at, at))
    return sorted(placed, key=lambda t: (t.start, t.index))


def delivery_order(timeline: Sequence[TimedItem]) -> list[TimedItem]:
    """The items in the order they are delivered (by ``send_at``)."""
    return sorted(timeline, key=lambda t: (t.send_at, t.index))


def parse_stop_after(value: str) -> StopAfter:
    """``90`` or ``90s`` is a meeting time in seconds; ``5turns`` (or ``5t``) a turn count."""
    text = value.strip().lower()
    match = re.fullmatch(r"(\d+)\s*(t|turn|turns)", text)
    if match:
        turns = int(match.group(1))
        if turns < 1:
            raise ValueError("--stop-after needs at least one turn")
        return StopAfter(turns=turns)
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*s?", text)
    if match:
        return StopAfter(seconds=float(match.group(1)))
    raise ValueError(f"--stop-after {value!r}: give seconds (90, 90s) or a turn count (5turns)")


def truncate(
    timeline: Sequence[TimedItem], stop: StopAfter | None
) -> tuple[list[TimedItem], float]:
    """The items delivered before the stop, and the meeting time the delivery ends at."""
    ordered = delivery_order(timeline)
    if stop is None:
        return ordered, max(t.send_at for t in ordered)
    if stop.turns is not None:
        turns = [t for t in ordered if t.turn]
        cutoff = turns[min(stop.turns, len(turns)) - 1].send_at
    else:
        cutoff = float(stop.seconds or 0.0)
    kept = [t for t in ordered if t.send_at <= cutoff]
    return kept, cutoff


def resolve_speed(via: str, pace: str | None, speed: float | None) -> float | None:
    """The speed-up factor, or None for no waiting at all.

    ``--speed`` wins. Otherwise ``live`` is 1. ``fast`` is no waiting on the
    Kinesis path and AUDIO_FAST_SPEED on the audio path. The default pace is fast
    for Kinesis and live for audio.
    """
    if speed is not None:
        if not math.isfinite(speed) or speed <= 0:
            raise ValueError("--speed must be a positive number")
        return float(speed)
    pace = pace or ("live" if via == "audio" else "fast")
    if pace == "live":
        return 1.0
    if pace == "fast":
        return AUDIO_FAST_SPEED if via == "audio" else None
    raise ValueError(f"unknown pace {pace!r}")


def wall_offset(meeting_time: float, speed: float | None) -> float:
    """Seconds after the delivery starts at which something at ``meeting_time`` is sent."""
    return 0.0 if not speed else meeting_time / speed


# --- the records ------------------------------------------------------------


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_call_id(meeting: Meeting, when: datetime, token: str) -> str:
    """``<title> - <UTC time> synthetic-<token>``.

    LMA names a meeting by its CallId and its own capture paths use
    ``<topic> - <time>``, so the meetings list shows the script's title; the
    ``synthetic-`` part marks it as the kit's and keeps it unique.
    """
    return (
        f"{meeting.title} - {when.astimezone(timezone.utc):%Y-%m-%d %H:%M:%S} UTC synthetic-{token}"
    )


def _remote_names(meeting: Meeting) -> str:
    names = [s.name for s in meeting.speakers if s.channel == REMOTE]
    if not names:
        return "Meeting participants"
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _local_name(meeting: Meeting) -> str:
    names = [s.name for s in meeting.speakers if s.channel == LOCAL]
    return names[0] if names else "Synthetic meeting kit"


def parse_extra(pairs: Sequence[str]) -> dict[str, Any]:
    """``KEY=VALUE`` pairs; a VALUE that is JSON (true, 3, [..], {..}) is decoded."""
    extra: dict[str, Any] = {}
    for pair in pairs:
        key, sep, raw = pair.partition("=")
        if not sep or not key.strip():
            raise ValueError(f"--start-event-extra {pair!r}: expected KEY=VALUE")
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            value = raw
        extra[key.strip()] = value
    return extra


def unknown_start_fields(extra: Mapping[str, Any]) -> list[str]:
    return sorted(set(extra) - START_FIELDS - EVENT_FIELDS)


def start_event(
    meeting: Meeting,
    call_id: str,
    owner: str,
    created_at: str,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The START record, shaped as live_guidance_probe and the Virtual Participant send it.

    The access token is empty, so the call event processor makes ``AgentId`` the
    meeting's Owner. ``extra`` is merged last and wins.
    """
    record: dict[str, Any] = {
        "EventType": "START",
        "CallId": call_id,
        "CustomerPhoneNumber": _remote_names(meeting),
        "SystemPhoneNumber": "LMA synthetic meeting kit",
        "AgentId": owner,
        "CreatedAt": created_at,
        "AccessToken": "",
        "IdToken": "",
        "RefreshToken": "",
        "Labels": [{"Key": LABEL_KEY, "Value": meeting.name}],
        "MeetingMetadataUpdatedAt": created_at,
    }
    if meeting.agenda:
        record.update(
            Agenda=meeting.agenda,
            AgendaSource="scheduled",
            AgendaUpdatedBy=owner,
            AgendaUpdatedAt=created_at,
        )
    if meeting.meeting_context:
        record.update(
            MeetingContext=meeting.meeting_context,
            MeetingContextUpdatedBy=owner,
            MeetingContextUpdatedAt=created_at,
        )
    record.update(extra or {})
    return record


def segment_id(call_id: str, item: TimedItem) -> str:
    return f"{call_id}-{'chat' if item.kind == 'chat' else 'turn'}-{item.index:03d}"


def segment_event(call_id: str, item: TimedItem, sent_at: str) -> dict[str, Any]:
    """An ADD_TRANSCRIPT_SEGMENT record for a turn or a chat message.

    A chat message is the record the Virtual Participant's sendChatSegment writes:
    channel MEETING_CHAT, final, StartTime equal to EndTime, the sender as Speaker.
    """
    return {
        "EventType": "ADD_TRANSCRIPT_SEGMENT",
        "CallId": call_id,
        "Channel": item.channel,
        "SegmentId": segment_id(call_id, item),
        "StartTime": item.start,
        "EndTime": item.end,
        "Transcript": item.text,
        "IsPartial": False,
        "CreatedAt": sent_at,
        "UpdatedAt": sent_at,
        "Sentiment": None,
        "TranscriptEvent": None,
        "Speaker": item.name,
        "AccessToken": "",
        "IdToken": "",
        "RefreshToken": "",
    }


def end_event(call_id: str, sent_at: str) -> dict[str, Any]:
    return {
        "EventType": "END",
        "CallId": call_id,
        "CreatedAt": sent_at,
        "AccessToken": "",
        "IdToken": "",
        "RefreshToken": "",
    }


def websocket_start_frame(
    meeting: Meeting, call_id: str, rate: int = SAMPLE_RATE
) -> dict[str, Any]:
    """The transcriber's START control message for this meeting.

    Diarization is asked for on a channel that carries more than one speaker. No
    ``channels`` field: the transcriber keeps its per-channel speaker map under
    that name, and a client value breaks it (see ws_stream_probe).
    """
    remote = [s for s in meeting.speakers if s.channel == REMOTE]
    local = [s for s in meeting.speakers if s.channel == LOCAL]
    return {
        "callEvent": "START",
        "callId": call_id,
        "remoteSpeakerName": _remote_names(meeting) if len(remote) == 1 else "Meeting participants",
        "localSpeakerName": _local_name(meeting) if len(local) <= 1 else "Local participants",
        "samplingRate": rate,
        "diarizeSystemChannel": len(remote) > 1,
        "diarizeMicChannel": len(local) > 1,
    }


def websocket_end_frame(call_id: str) -> dict[str, Any]:
    return {"callEvent": "END", "callId": call_id}


# --- audio -------------------------------------------------------------------


def split_for_polly(text: str, limit: int = POLLY_TEXT_LIMIT) -> list[str]:
    """Split a long turn at sentence ends into pieces Polly accepts."""
    if len(text) <= limit:
        return [text]
    pieces: list[str] = []
    current = ""
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        while len(sentence) > limit:  # one sentence longer than the limit: cut at a space
            if current:
                pieces.append(current)
                current = ""
            cut = sentence.rfind(" ", 0, limit)
            cut = cut if cut > 0 else limit
            pieces.append(sentence[:cut])
            sentence = sentence[cut:].lstrip()
        if current and len(current) + 1 + len(sentence) > limit:
            pieces.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        pieces.append(current)
    return pieces


def pcm_seconds(pcm: bytes, rate: int = SAMPLE_RATE) -> float:
    return len(pcm) / (SAMPLE_WIDTH * rate)


def assemble_stereo(
    timeline: Sequence[TimedItem],
    pcm: Mapping[int, bytes],
    rate: int = SAMPLE_RATE,
    end_at: float | None = None,
) -> bytes:
    """Interleaved 16-bit stereo: REMOTE turns on the left (ch_0), LOCAL on the right (ch_1).

    Each turn's mono PCM starts at its timeline start; everything else is
    silence. ``end_at`` cuts the audio at a meeting time (``--stop-after``);
    otherwise it runs TAIL_SECONDS past the last turn.
    """
    turns = [t for t in timeline if t.turn]
    last = max((t.start + pcm_seconds(pcm.get(t.index, b""), rate) for t in turns), default=0.0)
    total = int(round((end_at if end_at is not None else last + TAIL_SECONDS) * rate))
    left = array.array("h", bytes(total * SAMPLE_WIDTH))
    right = array.array("h", bytes(total * SAMPLE_WIDTH))
    for item in turns:
        samples = array.array("h")
        samples.frombytes(pcm.get(item.index, b""))
        if sys.byteorder == "big":
            samples.byteswap()
        offset = int(round(item.start * rate))
        if offset >= total:
            continue
        samples = samples[: total - offset]
        track = left if item.channel == REMOTE else right
        track[offset : offset + len(samples)] = samples
    stereo = array.array("h", bytes(total * 2 * SAMPLE_WIDTH))
    stereo[0::2] = left
    stereo[1::2] = right
    if sys.byteorder == "big":
        stereo.byteswap()
    return stereo.tobytes()


def wav_bytes(stereo: bytes, rate: int = SAMPLE_RATE) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(SAMPLE_WIDTH)
        w.setframerate(rate)
        w.writeframes(stereo)
    return out.getvalue()


def audio_chunks(
    stereo: bytes, rate: int = SAMPLE_RATE, chunk_ms: int = CHUNK_MS
) -> Iterator[tuple[float, bytes]]:
    """``(meeting time, bytes)`` for each chunk of the stereo audio."""
    frame_bytes = 2 * SAMPLE_WIDTH
    per_chunk = int(rate * chunk_ms / 1000) * frame_bytes
    for offset in range(0, len(stereo), per_chunk):
        yield offset / (frame_bytes * rate), stereo[offset : offset + per_chunk]


# --- cleanup keys --------------------------------------------------------------


def meeting_partitions(call_id: str) -> list[str]:
    return [f"{prefix}{call_id}" for prefix in MEETING_PARTITION_PREFIXES]


def list_row_key(call_id: str, created_at: str) -> tuple[str, str]:
    """The meetings-list row createCall writes beside the meeting (see its VTL in lma-ai-stack.yaml)."""
    shard = int(created_at[11:13]) // 4
    return f"cls#{created_at[:10]}#s#{shard:02d}", f"ts#{created_at}#id#{call_id}"


def posix_name(call_id: str) -> str:
    """The file-name form of a CallId the recordings and transcripts in S3 are named with."""
    return re.sub(r"_+$", "", re.sub(r"^_+", "", re.sub(r"[^a-zA-Z0-9_.]", "_", call_id)))


@dataclass
class Summary:
    """What a run sent, for the closing report."""

    call_id: str
    turns: int = 0
    chats: int = 0
    ended: bool = False
    meeting_seconds: float = 0.0
    wall_seconds: float = 0.0
    speakers: dict[str, int] = field(default_factory=dict)
