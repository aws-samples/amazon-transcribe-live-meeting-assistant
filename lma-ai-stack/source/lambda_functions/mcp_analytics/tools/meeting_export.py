# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""Tool: get_meeting_export -- a meeting as one structured record (schema version 1).

The export is built by the meeting archive function, which builds the same
record for the meeting page's download and the Everything archive, so there is
one builder. It is invoked with the MCP caller's identity and applies the
``getCall`` rule to it; the rule is applied here first as well, so a refusal
says the same as every other meeting tool's. A large export is answered with a
link to it signed for ten minutes instead. See docs/meeting-export.md and
docs/schemas/meeting-export-v1.schema.json.
"""

import os
from typing import Any, Dict, List, Optional

from tools.access import Caller, invoke_function, require_meeting, resolver_identity

#: The parts ``include`` takes (export.PARTS in the meeting archive function).
PARTS = (
    "transcript",
    "chat",
    "summary",
    "checklist",
    "consent",
    "actionItems",
    "keyframes",
    "guidance",
)

#: A meeting with hours of transcript is read and assembled within this.
READ_TIMEOUT_SECONDS = 300


def include_argument(value: Any) -> Optional[List[str]]:
    """``include`` checked: None for the default (every part)."""
    if value is None or value == []:
        return None
    if not isinstance(value, list) or not all(isinstance(part, str) for part in value):
        raise ValueError(f"include must be a list of: {', '.join(PARTS)}")
    unknown = [part for part in value if part not in PARTS]
    if unknown:
        raise ValueError(f"unknown part {unknown[0]!r}; include takes: {', '.join(PARTS)}")
    return list(dict.fromkeys(value))


def execute(caller: Caller, meeting_id: Any, include: Any = None) -> Dict[str, Any]:
    """get_meeting_export: the export, or ``{exportUrl, expiresAt, sizeBytes}`` when large."""
    parts = include_argument(include)
    meeting = require_meeting(meeting_id, caller)
    call_id = meeting.get("CallId") or str(meeting_id).strip()
    event: Dict[str, Any] = {
        "Trigger": "export",
        "CallId": call_id,
        "identity": resolver_identity(caller),
    }
    if parts is not None:
        event["Include"] = parts
    answer = invoke_function(
        os.environ.get("MEETING_ARCHIVE_FUNCTION", ""), event, READ_TIMEOUT_SECONDS
    )
    if not isinstance(answer, dict):
        raise ValueError("The meeting export could not be prepared")
    if isinstance(answer.get("export"), dict):
        return answer["export"]
    if answer.get("exportUrl"):
        return {
            "exportUrl": answer["exportUrl"],
            "expiresAt": answer.get("expiresAt"),
            "sizeBytes": answer.get("sizeBytes"),
        }
    raise ValueError("The meeting export could not be prepared")
