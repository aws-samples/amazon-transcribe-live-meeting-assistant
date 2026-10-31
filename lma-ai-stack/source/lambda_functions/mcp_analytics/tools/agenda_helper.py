# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""The optional agenda an MCP client passes when it starts or schedules a VP."""

from typing import Any, Optional

# Same bound as the createVirtualParticipant and updateCallAgenda resolvers.
MAX_AGENDA_CHARS = 10000


def normalize_agenda(agenda: Any) -> Optional[str]:
    """The agenda as stored text, one item per line, or None when there is none.

    The tool schema declares a string, which is what a calendar invite's agenda
    is. A list of strings is accepted as well, one item each, because that is
    the other shape an agent naturally produces for "the agenda items"; anything
    else is refused rather than stored as its repr.

    Raises ValueError for an agenda longer than the meeting record accepts, so
    the client is told to shorten it instead of having the whole start refused
    by the resolver with a less specific message.
    """
    if agenda is None:
        return None
    if isinstance(agenda, list):
        if not all(isinstance(item, str) for item in agenda):
            raise ValueError("agenda must be a string, or a list of strings")
        agenda = "\n".join(item.strip() for item in agenda if item.strip())
    if not isinstance(agenda, str):
        raise ValueError("agenda must be a string, or a list of strings")
    agenda = agenda.strip()
    if not agenda:
        return None
    if len(agenda) > MAX_AGENDA_CHARS:
        raise ValueError(f"agenda is longer than {MAX_AGENDA_CHARS} characters; shorten it")
    return agenda
