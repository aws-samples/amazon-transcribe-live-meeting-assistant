# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Sharing a meeting with the people on its participant list (Meeting Sharing).

The meetings table's stream carries every write of a meeting record. For each
one, ``plan_auto_share`` compares the record's participant list with its
sharing list and with ``AutoSharedWith``, the addresses this function has
shared the meeting with, and says what to change:

- an address on the list that is neither shared with nor in AutoSharedWith is
  shared with, and recorded in AutoSharedWith;
- an address in AutoSharedWith that is no longer on the list is unshared, when
  it is still shared with, and dropped from AutoSharedWith;
- an address the owner shared with by hand (shared with, not in
  AutoSharedWith) is never touched, on the list or off it;
- an address in AutoSharedWith that the owner unshared by hand, and that is
  still on the list, is left unshared: it stays in AutoSharedWith so that it is
  not shared with again until it is taken off the list and put back;
- the meeting's owner is never added.

Addresses are compared ignoring case, as the participant list compares them;
an address is shared with as it is written on the list.

Pure: no AWS calls. ``index.handle_meeting_stream`` reads the record, applies
the plan through the meeting sharing the share dialog uses, and writes
AutoSharedWith.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, List, Mapping, Optional, Sequence

#: The meeting record attribute that lists the addresses auto-share added.
AUTO_SHARED_ATTRIBUTE = "AutoSharedWith"

#: One recipient of a share: an email address. Every address it matches is one
#: the share dialog's check (index.RECIPIENT_PATTERN) accepts too, and it also
#: refuses white space and ",": "[", "]" and "," only ever separate SharedWith
#: entries, so an address containing one is not an entry that can be stored.
RECIPIENT_PATTERN = re.compile(r"[^@\[\],\s]+@[^@\[\],\s]+\.[^@\[\],\s]+")


def _key(address: str) -> str:
    return address.strip().casefold()


def listed_addresses(participants: Any) -> List[str]:
    """The email addresses on a stored participant list, in its order, each once."""
    addresses: List[str] = []
    seen = set()
    if not isinstance(participants, Sequence) or isinstance(participants, (str, bytes)):
        return addresses
    for entry in participants:
        if not isinstance(entry, Mapping):
            continue
        email = entry.get("email")
        if not isinstance(email, str) or not email.strip():
            continue
        # Only an address the share dialog would accept; the list's own
        # validation refuses any other, and one that reached the record another
        # way is not shared with.
        if not RECIPIENT_PATTERN.fullmatch(email.strip()):
            continue
        if _key(email) in seen:
            continue
        seen.add(_key(email))
        addresses.append(email.strip())
    return addresses


def _strings(values: Any) -> List[str]:
    if isinstance(values, (list, tuple, set, frozenset)):
        return [value.strip() for value in values if isinstance(value, str) and value.strip()]
    return []


@dataclass(frozen=True)
class AutoSharePlan:
    """What to change on one meeting."""

    add: List[str] = field(default_factory=list)
    remove: List[str] = field(default_factory=list)
    #: AutoSharedWith once the change is made.
    auto_shared: List[str] = field(default_factory=list)
    #: AutoSharedWith as it was, to tell whether it changed.
    auto_shared_before: List[str] = field(default_factory=list)

    @property
    def changes_sharing(self) -> bool:
        return bool(self.add or self.remove)

    @property
    def changes_anything(self) -> bool:
        before = sorted(_key(a) for a in self.auto_shared_before)
        after = sorted(_key(a) for a in self.auto_shared)
        return self.changes_sharing or before != after


def plan_auto_share(
    owner: Optional[str],
    shared_with: Iterable[str],
    participants: Any,
    auto_shared: Any,
) -> AutoSharePlan:
    """The change auto-share makes to a meeting, from its stored attributes.

    ``shared_with`` is the meeting's SharedWith entries (shared_with_entries),
    ``participants`` its stored Participants and ``auto_shared`` its stored
    AutoSharedWith.
    """
    owner_key = _key(owner) if isinstance(owner, str) else ""
    listed = {_key(a): a for a in listed_addresses(participants) if _key(a) != owner_key}
    shared = {_key(a): a for a in _strings(list(shared_with))}
    before = _strings(auto_shared)
    handled = {_key(a): a for a in before}

    add = [address for key, address in listed.items() if key not in handled and key not in shared]
    remove = [shared[key] for key in handled if key not in listed and key in shared]
    kept = [address for key, address in handled.items() if key in listed]
    return AutoSharePlan(add=add, remove=remove, auto_shared=kept + add, auto_shared_before=before)


def shared_with_after(shared_with: Iterable[str], plan: AutoSharePlan) -> List[str]:
    """The meeting's SharedWith once the plan is applied: its entries, less removals, plus additions."""
    removed = {_key(a) for a in plan.remove}
    kept = [a for a in _strings(list(shared_with)) if _key(a) not in removed]
    present = {_key(a) for a in kept}
    return kept + [a for a in plan.add if _key(a) not in present]


def list_row_key(call_id: str, created_at: str) -> Optional[Mapping[str, str]]:
    """The key of the meeting's row in the meetings list, which createCall writes beside it.

    ``cls#<date>#s#<shard>`` / ``ts#<CreatedAt>#id#<CallId>``, the shard being
    the four-hour block of the day CreatedAt falls in, as createCall computes it.
    None when CreatedAt is not an ISO date and time.
    """
    if not isinstance(created_at, str) or len(created_at) < 13 or not created_at[11:13].isdigit():
        return None
    shard = f"{int(created_at[11:13]) // 4:02d}"
    return {
        "PK": f"cls#{created_at[:10]}#s#{shard}",
        "SK": f"ts#{created_at}#id#{call_id}",
    }
