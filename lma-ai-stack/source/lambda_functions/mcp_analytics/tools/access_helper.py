# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""Who may read a meeting: its owner, a person it is shared with, and Admins.

The older tools (get_meeting_transcript, get_meeting_summary, list_meetings,
search_lma_meetings) check the owner only; they are not changed here. A tool
that uses ``can_read_meeting`` also answers people a meeting is shared with,
as the web app does.
"""

from typing import Any, Dict, Optional


# BEGIN shared_with_entries -- one text in every Lambda that reads SharedWith;
# lma-ai-stack/test/test_shared_with_access.py holds the copies to it.
# The entries of a SharedWith value, read as the AppSync resolvers read them
# (the "SharedWith entries" VTL block and sharedWithEntries in the JS
# resolvers): a list, tuple or set holds one entry per string element, and a
# string -- comma-separated or "[a, b]" -- is split at every "[", "]" and ",".
# Those three characters only ever separate entries, so a list element that
# contains one is not an entry. Entries are trimmed and empty ones dropped;
# membership is then a comparison of whole entries.
def shared_with_entries(value: object) -> list:
    """The trimmed, non-empty entries of a SharedWith value."""
    if isinstance(value, str):
        parts = value.replace("[", ",").replace("]", ",").split(",")
    elif isinstance(value, (list, tuple, set, frozenset)):
        parts = [
            part
            for part in value
            if isinstance(part, str) and not any(separator in part for separator in "[],")
        ]
    else:
        return []
    return [part.strip() for part in parts if part.strip()]


# END shared_with_entries


def can_read_meeting(
    meeting: Dict[str, Any], user_id: Optional[str], is_admin: bool = False
) -> bool:
    """Whether the caller may read this meeting row.

    Admins may read every meeting. Anyone else needs an identity and must be
    named by ``Owner`` or by ``AgentId`` (either one, whatever the other holds,
    as list_meetings decides), or be a whole entry of ``SharedWith``. A caller
    with no identity reads nothing. The event processor sets ``Owner`` from the
    caller's token, and from ``AgentId`` when there is no token; rows written
    before ``Owner`` existed carry ``AgentId`` only.
    """
    if is_admin:
        return True
    if not user_id:
        return False
    if user_id in (meeting.get("Owner"), meeting.get("AgentId")):
        return True
    return user_id in shared_with_entries(meeting.get("SharedWith"))
