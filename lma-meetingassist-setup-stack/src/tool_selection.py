#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""The meeting assistant's per-item tool selection, as the configuration profile holds it.

``meeting_assist.toolSelection`` in the active configuration profile names
individual tools by kind and id, each with three flags::

    {
      "mcpServers": {"<ServerId>": {"enabled": true, "inMeetingRequests": false, "alwaysOn": false}},
      "skills": {}
    }

- ``mcpServers`` is keyed by the ServerId of an installed MCP server. The
  registry itself, with each server's credentials, stays in the MCP servers
  table; the profile holds only these flags.
- ``skills`` has the same shape, keyed by the SkillId of a skill in the skills
  table (see meeting_skills.py). The skills themselves stay in that table.

The flags, each optional:

- ``enabled``: the assistant may use the item at all. Absent means true.
- ``inMeetingRequests``: the item may be used for a request made in the meeting
  (the meeting chat, the voice assistant, the wake phrase). Absent means the
  request's own default: the In-meeting requests setting's default for the kind
  (Unrestricted, or under Selected tools the kind's switch: ``inMeetingMcpServers``
  for MCP servers, ``inMeetingSkills`` for skills). Never used under Current meeting only, which offers nothing
  beyond the current meeting.
- ``alwaysOn``: reserved for a later always-on mode. Stored, and validated by
  the section schema, but not read.

An item the profile does not name behaves as it did before the profile could
name it, so a profile without ``toolSelection`` entries offers what it always
did. This module holds no I/O: ``parse`` reads the stored value, and the
meeting assistant decides with ``enabled`` and ``in_meeting_flag``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping, Optional

KIND_MCP_SERVERS = "mcpServers"
KIND_SKILLS = "skills"
#: Every kind the profile can name, in the order the schema lists them.
KINDS = (KIND_MCP_SERVERS, KIND_SKILLS)

FLAG_ENABLED = "enabled"
FLAG_IN_MEETING_REQUESTS = "inMeetingRequests"
FLAG_ALWAYS_ON = "alwaysOn"
#: Every flag an item can carry.
FLAGS = (FLAG_ENABLED, FLAG_IN_MEETING_REQUESTS, FLAG_ALWAYS_ON)


def _frozen(entries: Mapping[str, Mapping[str, Mapping[str, bool]]]) -> Mapping[str, Any]:
    return MappingProxyType(
        {
            kind: MappingProxyType({item: MappingProxyType(dict(flags)) for item, flags in items.items()})
            for kind, items in entries.items()
        }
    )


@dataclass(frozen=True)
class ToolSelection:
    """The per-item flags of one profile: kind -> item id -> flag -> bool.

    Only flags that hold a boolean are kept. Anything else -- a kind or flag
    this release does not know, a flag that is not true or false -- is left out,
    so the item behaves as though the flag were not set. The section schema
    refuses such a value at the save; this is for one that reached the profile
    another way (a restore of an older revision, say).
    """

    entries: Mapping[str, Mapping[str, Mapping[str, bool]]] = field(
        default_factory=lambda: _frozen({})
    )

    @classmethod
    def parse(cls, value: Any) -> "ToolSelection":
        """The selection a stored ``toolSelection`` value holds; empty for anything else."""
        entries = {}
        if isinstance(value, Mapping):
            for kind in KINDS:
                items = value.get(kind)
                if not isinstance(items, Mapping):
                    continue
                kept = {}
                for item_id, flags in items.items():
                    if not isinstance(item_id, str) or not item_id or not isinstance(flags, Mapping):
                        continue
                    kept[item_id] = {
                        flag: flags[flag] for flag in FLAGS if isinstance(flags.get(flag), bool)
                    }
                if kept:
                    entries[kind] = kept
        return cls(_frozen(entries))

    def names(self, kind: str, item_id: str) -> bool:
        """The profile has an entry for the item, whatever its flags."""
        return item_id in self.entries.get(kind, {})

    def flag(self, kind: str, item_id: str, flag: str) -> Optional[bool]:
        """The item's flag as the profile sets it, or None when it does not."""
        return self.entries.get(kind, {}).get(item_id, {}).get(flag)

    def enabled(self, kind: str, item_id: str) -> bool:
        """The assistant may use the item at all: true unless the profile turns it off."""
        return self.flag(kind, item_id, FLAG_ENABLED) is not False

    def in_meeting_flag(self, kind: str, item_id: str) -> Optional[bool]:
        """The item's own inMeetingRequests flag, or None to follow the request's default."""
        return self.flag(kind, item_id, FLAG_IN_MEETING_REQUESTS)

    def any_allowed_in_meeting(self, kind: str) -> bool:
        """Some enabled item of ``kind`` is allowed in-meeting requests by a flag of its own."""
        return any(
            self.enabled(kind, item_id) and self.in_meeting_flag(kind, item_id) is True
            for item_id in self.entries.get(kind, {})
        )


#: The selection of a profile that names no item.
EMPTY_SELECTION = ToolSelection()
