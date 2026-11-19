# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""Who is calling an MCP tool, and what they may do with a meeting.

Every meeting tool answers the callers the meeting page's getCall answers: an
Admin, the meeting's owner, or a user the meeting is shared with
(getCall.response.vtl). A few operations narrow that to the owner or an Admin,
as their own resolvers do: deleting and sharing a meeting
(meeting_controls_resolver.verify_permissions) and reading its consent record's
evidence. A Virtual Participant is read on the same rule against its own row,
and changed by its owner or an Admin only, as the Virtual Participant Manager's
``caller_may_change`` decides.

The server calls AppSync and the other Lambdas with its own IAM role, which
those resolvers treat as a trusted backend, so these checks run here first,
before any of those calls. Where a resolver Lambda decides from the caller's
identity (sharing, deleting, regenerating a summary), it is invoked directly
with the MCP caller's identity as well, so its own rule applies too.

A meeting that does not exist and one the caller may not see are refused with
the same message, so a refusal does not tell the caller that a meeting exists.
"""

import json
import os
from dataclasses import dataclass
from typing import Any, Dict, Optional

import boto3
from botocore.config import Config as BotoCoreConfig

from tools.get_virtual_participant_status import shared_with_entries

# The scopes an MCP API key carries (mcp_api_key_authorizer). A new key is READ
# or READ_WRITE; which tools each scope reaches is decided in index.scope_allows.
READ = "read"
READ_WRITE = "read-write"
# A key stored before keys had scopes: the original tools and the read tools.
LEGACY = "legacy"
SCOPES = (READ, READ_WRITE, LEGACY)

# The statuses of a meeting that is still being captured.
LIVE_STATUSES = ("STARTED", "TRANSCRIBING")

# Virtual Participant statuses after which it is no longer in, or headed for,
# a meeting.
VP_FINISHED_STATUSES = ("ENDED", "COMPLETED", "FAILED", "CANCELLED")

ADMIN_GROUP = os.environ.get("ADMIN_GROUP", "Admin")


@dataclass(frozen=True)
class Caller:
    """The user an MCP request was made for, as the API key resolved them."""

    user_id: str
    is_admin: bool = False
    scope: str = READ

    @property
    def may_write(self) -> bool:
        """Whether the caller's key allows tools that change something."""
        return self.scope == READ_WRITE


def normalize_scope(value: Any) -> str:
    """A stored or presented scope as one of SCOPES: none is LEGACY, anything unknown READ."""
    if value in (None, "", LEGACY):
        return LEGACY
    return READ_WRITE if value == READ_WRITE else READ


def _table(name_variable: str):
    return boto3.resource("dynamodb").Table(os.environ.get(name_variable, ""))


def calls_table():
    """The EventSourcing table: meetings, their transcripts and guidance."""
    return _table("CALLS_TABLE")


def meeting_key(call_id: str) -> Dict[str, str]:
    """The key of a meeting's row."""
    return {"PK": f"c#{call_id}", "SK": f"c#{call_id}"}


def get_meeting(call_id: str) -> Optional[Dict[str, Any]]:
    """The meeting's row, or None when there is none."""
    if not call_id:
        return None
    return calls_table().get_item(Key=meeting_key(call_id), ConsistentRead=True).get("Item")


def is_owner(row: Optional[Dict[str, Any]], caller: Caller) -> bool:
    """Whether the caller owns a meeting or Virtual Participant row."""
    if not row or not caller.user_id:
        return False
    return row.get("Owner") == caller.user_id


def can_read(row: Optional[Dict[str, Any]], caller: Caller) -> bool:
    """getCall's rule: an Admin, the owner, or an entry of SharedWith."""
    if not row:
        return False
    if caller.is_admin:
        return True
    if not caller.user_id:
        return False
    return is_owner(row, caller) or caller.user_id in shared_with_entries(row.get("SharedWith"))


def can_manage(row: Optional[Dict[str, Any]], caller: Caller) -> bool:
    """An Admin or the owner: sharing, deleting, consent evidence, VP changes."""
    return bool(row) and (caller.is_admin or is_owner(row, caller))


def not_found_or_denied(kind: str, item_id: str) -> PermissionError:
    """The one refusal for a missing row and a row the caller may not see."""
    return PermissionError(f"{kind} {item_id} was not found, or you do not have access to it")


def require_meeting(call_id: Any, caller: Caller) -> Dict[str, Any]:
    """The meeting's row for a caller getCall would answer; refused otherwise."""
    if not isinstance(call_id, str) or not call_id.strip():
        raise ValueError("meeting_id is required")
    call_id = call_id.strip()
    meeting = get_meeting(call_id)
    if meeting is None or not can_read(meeting, caller):
        raise not_found_or_denied("Meeting", call_id)
    return meeting


def require_meeting_manager(meeting: Dict[str, Any], caller: Caller, action: str) -> None:
    """Refuse an operation reserved for the meeting's owner and Admins."""
    if not can_manage(meeting, caller):
        raise PermissionError(f"Only the meeting's owner or an Admin may {action}")


def is_live(meeting: Dict[str, Any]) -> bool:
    """Whether the meeting is still being captured."""
    return meeting.get("Status") in LIVE_STATUSES


def list_keys(meeting: Dict[str, Any]) -> Dict[str, str]:
    """The ListPK and ListSK of a meeting's list row.

    The keys createCall wrote (CreateCallAppSyncResolver): the meeting's
    CreatedAt date and its four-hour shard of the day, and CreatedAt with the
    CallId. The UI derives them the same way (meeting-controls.js getListKeys).
    """
    created_at = str(meeting.get("CreatedAt") or "")
    call_id = meeting.get("CallId") or str(meeting.get("PK", "")).removeprefix("c#")
    if len(created_at) < 13:
        raise ValueError(f"Meeting {call_id} has no creation time to find its list entry by")
    shard = int(created_at[11:13]) // 4
    return {
        "ListPK": f"cls#{created_at[:10]}#s#{shard:02d}",
        "ListSK": f"ts#{created_at}#id#{call_id}",
    }


def resolver_identity(caller: Caller) -> Dict[str, Any]:
    """The caller as an AppSync Cognito identity, for a resolver Lambda invoked directly."""
    return {"username": caller.user_id, "groups": [ADMIN_GROUP] if caller.is_admin else []}


def invoke_function(function_name: str, payload: Dict[str, Any], read_timeout: int = 70) -> Any:
    """Invoke a Lambda synchronously and return its decoded result.

    Not retried: the functions invoked here (a meeting's deletion, a summary
    run, the meeting assistant) each act once per call, so a retry after a
    slow answer would act twice.
    """
    if not function_name:
        raise ValueError("This operation is not configured on this deployment")
    client = boto3.client(
        "lambda",
        config=BotoCoreConfig(
            read_timeout=read_timeout,
            connect_timeout=5,
            retries={"total_max_attempts": 1},
        ),
    )
    response = client.invoke(
        FunctionName=function_name,
        InvocationType="RequestResponse",
        Payload=json.dumps(payload, default=str).encode("utf-8"),
    )
    body = response["Payload"].read()
    result = json.loads(body) if body else None
    if response.get("FunctionError"):
        message = (result or {}).get("errorMessage") if isinstance(result, dict) else None
        raise ValueError(message or "The operation failed")
    return result
