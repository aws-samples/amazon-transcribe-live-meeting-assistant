# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Who spoke each transcript line: the meeting's speaker identity map, applied on read.

A transcript segment's ``Speaker`` is written once, by the transcriber, and is
never rewritten. On a diarized meeting it carries the name the meeting platform
showed (or the channel's label) plus the engine's voice label:
``Bob Strahan (spk_0)``. The meeting's ``SpeakerIdentities`` map says how those
are shown, and every reader that turns segments into named speakers applies it
through here, so the transcript on screen, the downloads, the summaries and the
meeting assistant all name the same people.

The map holds, keyed by ``<channel>#<speaker as stored>``:

* ``identities`` -- a name for a voice;
* ``merges`` -- a voice that is the same person as another;
* ``segmentOverrides`` -- a name for one segment alone;

and, keyed by ``<channel>#<name without its voice label>`` (a *group*):

* ``speakerModes`` -- whether the voices under one name are ONE PERSON (every
  line shows the name alone) or SEVERAL PEOPLE (each voice is shown, and can be
  named, on its own: ``ConfRm_1 · Voice 1``). With no entry, the mode is
  decided from the transcript (``speaker_groups``): several people when the name
  reads as a room or a device, or when its voices have been given different
  names; one person otherwise. How many voices a name has, and how long each
  spoke, do not decide it: speaker separation routinely splits one person into
  two or three voices that each speak at length. The rules are in
  ``speaker_mode_rules.json``.

Names that stand for nobody in particular -- ``Unknown``, the ``Other
Participant`` placeholder -- form no group and keep their voice labels, because
those labels are the only thing telling the people behind them apart.

The UI's twin is ``lma-ai-stack/source/ui/src/components/common/speaker-modes.js``
(with ``speaker-identities.js``); both are tested against
``test-fixtures/speaker-modes.json``. Pure Python with no dependencies beyond
``lma_channels`` in this layer.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Mapping, Optional

from lma_channels import REMOTE, canonical_channel, canonical_keyed, is_speech_channel

ONE_PERSON = "one"
SEVERAL_PEOPLE = "several"
MODES = (ONE_PERSON, SEVERAL_PEOPLE)

#: Why a group has the mode it has, when nobody chose one.
REASON_ROOM_NAME = "room-name"
REASON_NAMED_VOICES = "named-voices"
REASON_DEFAULT = "one-person"

#: The placeholder the transcriber writes for the remote side of a streaming
#: meeting before anyone has been named; the Stream Audio participant name stands
#: in for it.
DEFAULT_OTHER_SPEAKER_NAME = "Other Participant"

#: How an unnamed voice of a group shown as several people is labelled.
VOICE_SEPARATOR = " · "

DIARIZATION_LABEL = re.compile(r"^(.*?)(\s*\(spk_(\d+)\))$")

RULES: Dict[str, Any] = json.loads(
    (Path(__file__).resolve().parent / "speaker_mode_rules.json").read_text(encoding="utf-8")
)
UNKNOWN_NAMES = frozenset(name.casefold() for name in RULES["unknownNames"])
ROOM_NAME_PATTERNS = tuple(
    re.compile(entry["pattern"], re.IGNORECASE) for entry in RULES["roomNamePatterns"]
)


# ── the stored map ──────────────────────────────────────────────────────────


def parse_speaker_identities(raw: Any) -> Dict[str, Dict[str, Any]]:
    """Read the meeting's speaker identity map, tolerating anything.

    A meeting written before this feature has no attribute, and a blob from a
    newer version may carry keys this does not know. Both yield a usable map
    rather than an error, so every failure returns the empty one.
    """
    sections = ("identities", "merges", "segmentOverrides", "speakerModes")
    empty: Dict[str, Dict[str, Any]] = {section: {} for section in sections}
    if not raw:
        return empty
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        return empty
    if not isinstance(parsed, dict):
        return empty
    out = {
        section: parsed[section] if isinstance(parsed.get(section), dict) else {}
        for section in sections
    }
    # A map written before 0.4.0 keys its voices under the older channel names.
    out["identities"] = canonical_keyed(out["identities"])
    out["merges"] = canonical_keyed(out["merges"], values_are_keys=True)
    out["speakerModes"] = canonical_keyed(out["speakerModes"])
    return out


def split_diarization_label(speaker: Any) -> tuple:
    """A stored speaker's name and its voice label: ``("Bob", " (spk_0)")``."""
    value = speaker if isinstance(speaker, str) else ""
    match = DIARIZATION_LABEL.match(value)
    return (match.group(1), match.group(2)) if match else (value, "")


def speaker_key(channel: Any, speaker: Any) -> str:
    """Key for one voice: its channel, by its current name, plus the speaker as stored."""
    return f"{canonical_channel(channel) or ''}#{(speaker or '').strip()}"


def group_key(channel: Any, name: Any) -> str:
    """Key for the voices under one name on one channel: ``REMOTE#ConfRm_1``."""
    return f"{canonical_channel(channel) or ''}#{(name or '').strip()}"


def resolve_merge_target(key: str, merges: Mapping[str, Any]) -> str:
    """Follow a merge chain to the key that holds the name, stopping on a cycle."""
    current = key
    seen = {current}
    while isinstance(merges.get(current), str) and merges[current] not in seen:
        current = merges[current]
        seen.add(current)
    return current


def _group_of_key(key: str) -> str:
    channel, _, speaker = key.partition("#")
    base, _suffix = split_diarization_label(speaker)
    return group_key(channel, base)


# ── the default ─────────────────────────────────────────────────────────────


def is_unknown_name(name: Any) -> bool:
    """A name that stands for nobody in particular, whose voice labels are kept."""
    return (name if isinstance(name, str) else "").strip().casefold() in UNKNOWN_NAMES


def looks_like_room(name: Any) -> bool:
    """Whether a platform name reads as a room or a room device rather than a person."""
    value = (name if isinstance(name, str) else "").strip()
    return bool(value) and any(pattern.search(value) for pattern in ROOM_NAME_PATTERNS)


def _seconds(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _field(row: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in row:
            return row[name]
    return None


def speaker_groups(
    segments: Iterable[Mapping[str, Any]], identities: Mapping[str, Any]
) -> Dict[str, Dict[str, Any]]:
    """The meeting's names that carry voice labels, each with its voices and mode.

    Read from the final speech segments; a partial carries no voice label yet.
    Segments may use the stored attribute names (``Speaker``, ``StartTime``...)
    or the UI's (``speaker``, ``startTime``...).
    """
    groups: Dict[str, Dict[str, Any]] = {}
    for row in segments or []:
        if _field(row, "IsPartial", "isPartial") is True:
            continue
        channel = canonical_channel(_field(row, "Channel", "channel"))
        if not is_speech_channel(channel):
            continue
        stored = (_field(row, "Speaker", "speaker") or "").strip()
        base, suffix = split_diarization_label(stored)
        base = base.strip()
        if is_unknown_name(base):
            continue
        key = group_key(channel, base)
        start = _seconds(_field(row, "StartTime", "startTime"))
        duration = max(0.0, _seconds(_field(row, "EndTime", "endTime")) - start)
        group = groups.setdefault(
            key,
            {
                "key": key,
                "channel": channel,
                "base": base,
                "seconds": 0.0,
                "voices": {},
                "members": set(),
            },
        )
        group["seconds"] += duration
        group["members"].add(speaker_key(channel, stored))
        if suffix:
            voice_id = suffix.strip()[1:-1]
            voice = group["voices"].setdefault(
                voice_id,
                {
                    "id": voice_id,
                    "key": speaker_key(channel, stored),
                    "seconds": 0.0,
                    "first": start,
                },
            )
            voice["seconds"] += duration
            voice["first"] = min(voice["first"], start)

    names = identities.get("identities") or {}
    merges = identities.get("merges") or {}
    modes = identities.get("speakerModes") or {}
    for key, group in groups.items():
        voices = sorted(
            group["voices"].values(),
            key=lambda v: (v["first"], int(v["id"].split("_", 1)[1])),
        )
        for ordinal, voice in enumerate(voices, start=1):
            voice["ordinal"] = ordinal
        group["voices"] = voices

        # The names given to this group's voices (through any merge that stays
        # within the group). Voices merged into someone else resolve as that
        # person and take no part here.
        given: Dict[str, Dict[str, Any]] = {}
        for member in sorted(group["members"]):
            target = resolve_merge_target(member, merges)
            if _group_of_key(target) != key:
                continue
            entry = names.get(target)
            if (
                isinstance(entry, dict)
                and isinstance(entry.get("name"), str)
                and entry["name"].strip()
            ):
                given.setdefault(entry["name"].strip(), entry)

        # Neither the number of voices nor their share of the speech is a rule:
        # both are what a diarizer splitting one person produces.
        if looks_like_room(group["base"]):
            default, reason = SEVERAL_PEOPLE, REASON_ROOM_NAME
        elif len(given) >= 2:
            default, reason = SEVERAL_PEOPLE, REASON_NAMED_VOICES
        else:
            default, reason = ONE_PERSON, REASON_DEFAULT
        chosen = modes.get(key)
        override = chosen.get("mode") if isinstance(chosen, dict) else None
        group["default"] = default
        group["reason"] = reason
        group["override"] = override if override in MODES else None
        group["mode"] = group["override"] or default

        # The one name a group shown as one person goes by: the name its voices
        # were given, when they agree on one, else the platform name.
        if len(given) == 1:
            ((name, entry),) = given.items()
            group["name"] = name
            group["nameStatus"] = entry.get("status") or "confirmed"
            group["nameLocked"] = entry.get("locked") is True
        else:
            group["name"] = group["base"]
            group["nameStatus"] = None
            group["nameLocked"] = False
    return groups


# ── one line ────────────────────────────────────────────────────────────────


def voice_label(name: str, ordinal: int) -> str:
    """How an unnamed voice of a name shown as several people reads: ``ConfRm_1 · Voice 2``."""
    return f"{name}{VOICE_SEPARATOR}Voice {ordinal}"


def resolve_speaker(
    row: Mapping[str, Any],
    identities: Mapping[str, Any],
    fallback_name: Optional[str] = None,
    groups: Optional[Mapping[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Who spoke one segment, and how much to trust the name.

    Returns ``name``, ``status`` ("confirmed", "proposed" or "unnamed"),
    ``overridden`` (named for this segment alone), ``locked``, and ``group``
    (the group key, when the speaker belongs to one). ``groups`` is
    ``speaker_groups`` over the whole transcript; without it no group applies.

    Must agree with resolveSpeaker in the UI's speaker-identities.js for the same
    inputs: a divergence means the transcript on screen and the transcript the
    summary prompts read disagree about who spoke. One difference is deliberate:
    a segment with no stored speaker at all stays unattributed here, where the
    UI has to put something in its speaker column.
    """
    overrides = identities.get("segmentOverrides") or {}
    names = identities.get("identities") or {}
    merges = identities.get("merges") or {}
    stored = (_field(row, "Speaker", "speaker") or "").strip()
    channel = canonical_channel(_field(row, "Channel", "channel"))
    key = speaker_key(channel, stored)
    target = resolve_merge_target(key, merges)
    entry = names.get(target)
    locked = isinstance(entry, dict) and entry.get("locked") is True

    override = overrides.get(_field(row, "SegmentId", "segmentId"))
    if isinstance(override, str) and override:
        return {
            "name": override,
            "status": "confirmed",
            "overridden": True,
            "locked": locked,
            "group": None,
        }

    base, suffix = split_diarization_label(stored)
    gkey = group_key(channel, base.strip())
    group = (groups or {}).get(gkey)
    in_group = group is not None and _group_of_key(target) == gkey

    if in_group and group["mode"] == ONE_PERSON:
        return {
            "name": group["name"],
            "status": group["nameStatus"] or "unnamed",
            "overridden": False,
            "locked": group["nameLocked"],
            "group": gkey,
        }

    if isinstance(entry, dict) and isinstance(entry.get("name"), str) and entry["name"]:
        return {
            "name": entry["name"],
            "status": entry.get("status") or "confirmed",
            "overridden": False,
            "locked": locked,
            "group": gkey if in_group else None,
        }

    if in_group and suffix:
        target_id = split_diarization_label(target.partition("#")[2])[1].strip()[1:-1]
        ordinal = next((v["ordinal"] for v in group["voices"] if v["id"] == target_id), None)
        if ordinal is not None:
            return {
                "name": voice_label(group["base"], ordinal),
                "status": "unnamed",
                "overridden": False,
                "locked": False,
                "group": gkey,
            }

    # No mapping. Keep the voice label: two voices on one channel differ only by
    # it, so dropping it would read as one person in every summary.
    if base == DEFAULT_OTHER_SPEAKER_NAME and channel == REMOTE and fallback_name:
        name = f"{fallback_name}{suffix}"
    else:
        name = stored
    return {"name": name, "status": "unnamed", "overridden": False, "locked": locked, "group": None}


def resolve_speaker_name(
    row: Mapping[str, Any],
    identities: Mapping[str, Any],
    fallback_name: Optional[str] = None,
    groups: Optional[Mapping[str, Dict[str, Any]]] = None,
) -> str:
    """The display name for one segment; see ``resolve_speaker``."""
    return resolve_speaker(row, identities, fallback_name, groups)["name"]


def speaker_namer(
    segments: Iterable[Mapping[str, Any]],
    raw_identities: Any,
    fallback_name: Optional[str] = None,
) -> Callable[[Mapping[str, Any]], str]:
    """A function naming each of a meeting's segments, from its stored map.

    The groups are built from ``segments``, so pass the whole transcript even
    when only part of it is named: a name's voices, their Voice 1, Voice 2...
    order and the names they were given are properties of the meeting, not of
    the excerpt.
    """
    rows = list(segments or [])
    identities = parse_speaker_identities(raw_identities)
    groups = speaker_groups(rows, identities)
    return lambda row: resolve_speaker_name(row, identities, fallback_name, groups)


__all__ = [
    "DEFAULT_OTHER_SPEAKER_NAME",
    "MODES",
    "ONE_PERSON",
    "RULES",
    "SEVERAL_PEOPLE",
    "group_key",
    "is_unknown_name",
    "looks_like_room",
    "parse_speaker_identities",
    "resolve_merge_target",
    "resolve_speaker",
    "resolve_speaker_name",
    "speaker_groups",
    "speaker_key",
    "speaker_namer",
    "split_diarization_label",
    "voice_label",
]
