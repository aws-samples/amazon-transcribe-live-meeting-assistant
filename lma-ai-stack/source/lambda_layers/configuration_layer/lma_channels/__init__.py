# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""The names of a meeting transcript's channels: two speech channels and the meeting chat.

A meeting's audio arrives on two channels:

* ``REMOTE`` -- everyone else in the meeting: the browser tab or meeting app's
  audio, the Virtual Participant's view of the meeting, an uploaded recording.
* ``LOCAL`` -- the local side: the user's own microphone for Stream Audio and the
  Desktop Capture App, and the voice assistant's own speech for a Virtual
  Participant.

LMA versions before 0.4.0 wrote these as ``CALLER`` and ``AGENT``, names that
came from the telephony solution LMA started from. Segments, meeting records and
speaker-name maps written by those versions keep the old names until they
expire, so everything that READS a channel goes through ``canonical_channel``
and treats the two spellings as one channel. Everything that WRITES one writes
``REMOTE`` or ``LOCAL``.

A third channel, ``MEETING_CHAT``, holds the messages participants typed in the
meeting's chat, which a Virtual Participant reads and adds to the transcript.
It is not speech: nothing that reads what was said (sentiment, the meeting
assistant's wake phrase, speaker naming) reads it, which is why each of those
filters on ``is_speech_channel`` or ``STORED_SPEECH_CHANNELS`` rather than on
"every channel but the assistants'". A reader that wants the chat as well asks
for it: the summary does, through FetchTranscript's ``IncludeChat``, which marks
each typed line ``[chat]``.

The UI's twin is ``lma-ai-stack/source/ui/src/components/common/channels.js``.
The WebSocket transcriber and the Virtual Participant only write channels, so
their ``channels.ts`` modules hold the two names and no aliases.

Pure Python with no dependencies, so any function that attaches this layer can
import it, and tests can put the layer directory on ``sys.path``.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

REMOTE = "REMOTE"
LOCAL = "LOCAL"

# The two speech channels, as written.
SPEECH_CHANNELS = (REMOTE, LOCAL)

# Older spellings, still read. CALLER and AGENT are what LMA stored before 0.4.0.
# CUSTOMER is only ever an incoming name -- the participant role Amazon Transcribe
# uses for the non-agent side of a call -- and was never stored, which is why
# STORED_SPEECH_CHANNELS leaves it out.
CHANNEL_ALIASES: Dict[str, str] = {
    "CALLER": REMOTE,
    "CUSTOMER": REMOTE,
    "AGENT": LOCAL,
}

# Every stored value that is a speech channel. For a filter evaluated where
# ``canonical_channel`` cannot run, such as a DynamoDB FilterExpression.
STORED_SPEECH_CHANNELS = (REMOTE, LOCAL, "CALLER", "AGENT")

# Messages typed in the meeting chat, one segment per message.
MEETING_CHAT = "MEETING_CHAT"

# Every stored value that is something a participant said or typed: the speech
# channels under either spelling, and the meeting chat. For a reader that asks
# for chat as well as speech.
STORED_TRANSCRIPT_CHANNELS = STORED_SPEECH_CHANNELS + (MEETING_CHAT,)


def canonical_channel(channel: Optional[str]) -> Optional[str]:
    """The current name of a channel; any other value is returned unchanged."""
    if channel is None:
        return None
    return CHANNEL_ALIASES.get(channel, channel)


def is_speech_channel(channel: Optional[str]) -> bool:
    """Whether a stored or incoming channel is one of the two speech channels."""
    return canonical_channel(channel) in SPEECH_CHANNELS


def is_chat_channel(channel: Optional[str]) -> bool:
    """Whether a stored or incoming channel is the meeting chat."""
    return channel == MEETING_CHAT


def canonical_speaker_key(key: Any) -> Any:
    """A speaker-identity key (``<channel>#<speaker>``) under the current channel name.

    Keys that are not strings, or whose channel part is not an older spelling,
    are returned unchanged.
    """
    if not isinstance(key, str) or "#" not in key:
        return key
    channel, rest = key.split("#", 1)
    canonical = CHANNEL_ALIASES.get(channel)
    return f"{canonical}#{rest}" if canonical else key


def canonical_keyed(section: Mapping[str, Any], values_are_keys: bool = False) -> Dict[str, Any]:
    """A speaker-identity section re-keyed under the current channel names.

    For the ``identities``, ``rejected`` and ``merges`` sections of a meeting's
    SpeakerIdentities map. With ``values_are_keys`` the values are re-keyed too,
    as ``merges`` maps one speaker key to another.

    A meeting recorded across an upgrade can hold the same voice under both
    spellings. The entry already under the current name wins, because it was
    written later.
    """
    out: Dict[str, Any] = {}
    renamed: Dict[str, Any] = {}
    for key, value in section.items():
        new_value = canonical_speaker_key(value) if values_are_keys else value
        new_key = canonical_speaker_key(key)
        if new_key == key:
            out[key] = new_value
        else:
            renamed[new_key] = new_value
    for key, value in renamed.items():
        out.setdefault(key, value)
    return out


__all__ = [
    "CHANNEL_ALIASES",
    "LOCAL",
    "MEETING_CHAT",
    "REMOTE",
    "SPEECH_CHANNELS",
    "STORED_SPEECH_CHANNELS",
    "STORED_TRANSCRIPT_CHANNELS",
    "canonical_channel",
    "canonical_keyed",
    "canonical_speaker_key",
    "is_chat_channel",
    "is_speech_channel",
]
