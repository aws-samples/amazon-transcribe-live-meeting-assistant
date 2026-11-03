# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""The names of a meeting transcript's two speech channels.

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

# Older spellings, still read. CUSTOMER is the participant role Amazon
# Transcribe uses for the non-agent side of a call.
CHANNEL_ALIASES: Dict[str, str] = {
    "CALLER": REMOTE,
    "CUSTOMER": REMOTE,
    "AGENT": LOCAL,
}

# Every stored value that is a speech channel. For a filter evaluated where
# ``canonical_channel`` cannot run, such as a DynamoDB FilterExpression.
STORED_SPEECH_CHANNELS = (REMOTE, LOCAL, "CALLER", "AGENT")


def canonical_channel(channel: Optional[str]) -> Optional[str]:
    """The current name of a channel; any other value is returned unchanged."""
    if channel is None:
        return None
    return CHANNEL_ALIASES.get(channel, channel)


def is_speech_channel(channel: Optional[str]) -> bool:
    """Whether a stored or incoming channel is one of the two speech channels."""
    return canonical_channel(channel) in SPEECH_CHANNELS


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
    "REMOTE",
    "SPEECH_CHANNELS",
    "STORED_SPEECH_CHANNELS",
    "canonical_channel",
    "canonical_keyed",
    "canonical_speaker_key",
    "is_speech_channel",
]
