#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""
MCP Analytics Lambda Function
Implements MCP protocol tools for LMA meeting data access.

Two front doors serve the same tools: the seven original ones (MCP_TOOLS) and
the meeting tools in tool_definitions.py.

* The API key path speaks MCP JSON-RPC. Every API key carries a scope
  (execute_tool_call): a read key is answered only by the tools whose
  annotations declare readOnlyHint, a read-write key by every tool, and a legacy
  key -- one stored before keys had scopes -- by the seven original tools and
  the read-only meeting tools.
* The BedrockAgentCore Gateway path invokes this function with a tool's
  arguments as the event and the tool's name in the Lambda context
  (``bedrockAgentCoreToolName``). An event without that name is matched to one
  of the seven original tools by its arguments, as before.

Access to each meeting is decided in tools/access.py, on both paths.
"""

import json
import logging
import os
from typing import Any, Callable, Dict, List, NamedTuple, Optional

from tool_definitions import MEETING_TOOLS

# Import tool implementations
from tools import (
    access,
    ask_assistant,
    consent,
    get_summary,
    get_transcript,
    get_virtual_participant_status,
    guidance,
    list_meetings,
    meeting_actions,
    meeting_agenda,
    meeting_record,
    preparation,
    schedule_meeting,
    scheduled_meeting,
    search_meetings,
    start_meeting_now,
    virtual_participants,
    vp_preparation,
)

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

ADMIN_GROUP = os.environ.get("ADMIN_GROUP", "Admin")

# The scopes of an MCP API key (tools/access.py). A new key is generated with
# READ or READ_WRITE. A key stored before keys had scopes is LEGACY: it keeps
# every tool in MCP_TOOLS, the tools the server offered such keys, and of the
# meeting tools it gets the read-only ones. An authorizer context with no scope
# is a key of that kind too.
READ_SCOPE = "read"
READ_WRITE_SCOPE = "read-write"
LEGACY_SCOPE = "legacy"

# The gateway names a tool "<target name>___<tool name>".
GATEWAY_TOOL_SEPARATOR = "___"


def server_version() -> str:
    """The deployed LMA version (the stack's release), as serverInfo reports it."""
    return os.environ.get("LMA_VERSION") or "unknown"


def key_scope(authorizer: Dict[str, Any]) -> str:
    """The scope the API key authorizer resolved; any unknown value is read."""
    scope = authorizer.get("scope")
    if scope in (None, "", LEGACY_SCOPE):
        return LEGACY_SCOPE
    return READ_WRITE_SCOPE if scope == READ_WRITE_SCOPE else READ_SCOPE


# Invariant: every tool scopes its results to the calling user and widens that
# scope only for members of the admin group, so a request that does not resolve
# to a user cannot be answered. Both entry paths carry an identity: the REST
# endpoint through its authorizer context, the gateway through forwarded claims.
NO_IDENTITY_MESSAGE = (
    "Unable to determine the calling user. Connect through the per-user API key "
    "endpoint so that each request carries a user identity "
    "(see docs/mcp-api-key-auth.md)."
)


def is_admin_claim(claims: Dict[str, Any]) -> bool:
    """Whether the caller's `cognito:groups` claim contains the admin group.

    The claim arrives as a list from the Cognito user-pool authorizer and as a
    comma-joined string from some token shapes. Both are normalized to a list of
    group names so the comparison is an exact match on a whole group name.
    """
    groups = claims.get("cognito:groups") or []
    if isinstance(groups, str):
        groups = [group.strip() for group in groups.split(",") if group.strip()]
    elif not isinstance(groups, (list, tuple)):
        groups = []
    return ADMIN_GROUP in groups


def gateway_tool_name(context: Any) -> Optional[str]:
    """The tool the AgentCore Gateway was asked for, from the Lambda context; None without one."""
    client_context = getattr(context, "client_context", None)
    custom = getattr(client_context, "custom", None)
    if not isinstance(custom, dict):
        return None
    name = custom.get("bedrockAgentCoreToolName")
    if not isinstance(name, str) or not name:
        return None
    return name.split(GATEWAY_TOOL_SEPARATOR, 1)[-1]


def infer_tool(tool_input: Dict[str, Any]) -> str:
    """One of the seven original tools, guessed from the arguments of an unnamed call."""
    if "virtualParticipantId" in tool_input and len(tool_input) <= 2:
        return "get_virtual_participant_status"
    if "query" in tool_input and "maxResults" in tool_input:
        return "search_lma_meetings"
    if "meetingId" in tool_input and "format" in tool_input:
        return "get_meeting_transcript"
    if "meetingId" in tool_input and (
        "includeActionItems" in tool_input or "includeTopics" in tool_input
    ):
        return "get_meeting_summary"
    if "meetingPlatform" in tool_input and "scheduledTime" in tool_input:
        return "schedule_meeting"
    if (
        "meetingPlatform" in tool_input
        and "meetingName" in tool_input
        and "scheduledTime" not in tool_input
    ):
        return "start_meeting_now"
    # list_meetings, also when nothing else matches.
    return "list_meetings"


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Main handler for MCP Analytics Lambda.
    Routes tool calls to appropriate implementations.
    Enforces user-based access control (UBAC).
    """
    # An event carries caller claims or authorizer context, so the event body is
    # not logged; its shape is, and the resolved caller is logged on its own below.
    logger.info(f"Event keys: {sorted(event.keys())}")

    try:
        # Detect API Gateway proxy integration (API key auth path)
        if "httpMethod" in event and "body" in event:
            body = event.get("body", "{}")
            tool_input = json.loads(body) if isinstance(body, str) else (body or {})
            authorizer = event.get("requestContext", {}).get("authorizer", {})
            user_id = authorizer.get("userId")
            username = authorizer.get("username") or user_id
            is_admin = authorizer.get("isAdmin", "false") == "true"
            scope = key_scope(authorizer)

            # The API key authorizer resolves the key to a user before this
            # function runs, so a request without that context has no caller.
            if not user_id:
                logger.warning("Request carries no caller identity - rejecting")
                return error_response(403, NO_IDENTITY_MESSAGE)

            logger.info(f"API Gateway path - User: {username}, Admin: {is_admin}, Scope: {scope}")
            key_scope_to_apply = scope

            # Handle MCP JSON-RPC protocol messages
            if "jsonrpc" in tool_input and "method" in tool_input:
                return handle_mcp_jsonrpc(tool_input, user_id, username, is_admin, scope)
            tool_name = infer_tool(tool_input)
        else:
            # BedrockAgentCore Gateway path: the function is invoked with the raw
            # tool input, so the only identity available is whatever the event
            # itself carries in requestContext.authorizer.claims.
            request_context = event.get("requestContext", {})
            authorizer = request_context.get("authorizer", {})
            claims = authorizer.get("claims", {})

            logger.info(f"requestContext keys: {list(request_context.keys())}")
            logger.info(f"authorizer keys: {list(authorizer.keys())}")
            logger.info(f"claim keys: {sorted(claims.keys())}")

            user_id = claims.get("sub")
            username = claims.get("cognito:username", claims.get("email", user_id))
            is_admin = is_admin_claim(claims)

            # Clients that reach this function without claims should use the
            # per-user API key endpoint instead -- it supplies
            # userId/username/isAdmin on every call. See docs/mcp-api-key-auth.md.
            if not user_id:
                logger.warning("Request carries no caller identity - rejecting")
                return error_response(403, NO_IDENTITY_MESSAGE)

            tool_input = event
            tool_name = gateway_tool_name(context) or infer_tool(tool_input)
            # The gateway authenticates the user with OAuth; no API key, so no scope.
            key_scope_to_apply = None

        logger.info(f"User: {username}, ID: {user_id}, Admin: {is_admin}")
        # On the gateway path `tool_input` is the event itself, which carries the
        # caller's claims, so the argument names are logged rather than the values.
        logger.info(f"Tool: {tool_name}, Input keys: {sorted(tool_input.keys())}")

        if tool_name not in TOOL_HANDLERS:
            return error_response(400, f"Unknown tool: {tool_name}")

        # A request on the API key endpoint is held to its key's scope whatever
        # the shape of its body, as execute_tool_call holds a JSON-RPC call.
        if key_scope_to_apply is not None and not scope_allows(key_scope_to_apply, tool_name):
            logger.warning(f"Refused {tool_name} for a key with the {key_scope_to_apply} scope")
            return error_response(403, scope_refusal(tool_name, key_scope_to_apply))

        caller = CallerInfo(user_id, is_admin, key_scope_to_apply or READ_WRITE_SCOPE)
        result = TOOL_HANDLERS[tool_name](caller, tool_input)

        # Return MCP-formatted success response
        return success_response(result)

    except PermissionError as e:
        logger.warning(f"Permission denied: {e}")
        return error_response(403, str(e))

    except ValueError as e:
        logger.warning(f"Invalid input: {e}")
        return error_response(400, str(e))

    except Exception as e:
        logger.error(f"Error processing tool call: {e}", exc_info=True)
        return error_response(500, f"Internal error: {str(e)}")


MCP_TOOLS = [
    {
        "name": "search_lma_meetings",
        "description": "Search across all meeting transcripts and summaries using natural language queries",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Natural language search query"},
                "startDate": {"type": "string", "description": "Optional ISO 8601 start date"},
                "endDate": {"type": "string", "description": "Optional ISO 8601 end date"},
                "maxResults": {"type": "number", "description": "Maximum results to return"},
            },
            "required": ["query"],
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
    },
    {
        "name": "get_meeting_transcript",
        "description": (
            "Retrieve the transcript of a meeting. format text (the default) and srt return "
            "the whole transcript of an ended meeting. format json returns it page by page, "
            "also while the meeting runs: {segments, nextToken, meetingStatus}, each segment "
            "with segmentId, startTime, endTime, speaker, channel, text and type (speech or "
            "chat). Call it first without nextToken, then with the nextToken of the previous "
            "answer. While the meeting is in progress nextToken is always returned, so poll "
            "from it (about every pollAfterSeconds); once the meeting has ended and the last "
            "segment has been read it is null."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "meetingId": {"type": "string", "description": "Meeting ID (CallId)"},
                "format": {
                    "type": "string",
                    "description": "Output format: json, text (default) or srt",
                },
                "nextToken": {
                    "type": "string",
                    "description": (
                        "format json: the nextToken of the previous answer; omit it on the "
                        "first call"
                    ),
                },
                "maxSegments": {
                    "type": "number",
                    "description": "format json: most segments to return (default 500, at most 2000)",
                },
                "includeAssistant": {
                    "type": "boolean",
                    "description": (
                        "format json: also return the meeting assistant's answers (type "
                        "assistant; default false)"
                    ),
                },
            },
            "required": ["meetingId"],
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
    },
    {
        "name": "get_meeting_summary",
        "description": "Get AI-generated summary and action items for a meeting",
        "inputSchema": {
            "type": "object",
            "properties": {
                "meetingId": {"type": "string", "description": "Meeting ID (CallId)"},
                "includeActionItems": {"type": "boolean", "description": "Include action items"},
                "includeTopics": {"type": "boolean", "description": "Include key topics"},
            },
            "required": ["meetingId"],
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
    },
    {
        "name": "list_meetings",
        "description": (
            "List meetings with optional filters, newest first. Each meeting has its labels, "
            "audience, status, startTime, endTime, owner, summaryReady and, when a Virtual "
            "Participant captured it, virtualParticipantId."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "startDate": {"type": "string", "description": "ISO 8601 start date"},
                "endDate": {"type": "string", "description": "ISO 8601 end date"},
                "participant": {
                    "type": "string",
                    "description": (
                        "Matches a name in a participant list stored on the meeting record; "
                        "LMA does not record one today, so this usually matches nothing."
                    ),
                },
                "status": {
                    "type": "string",
                    "description": (
                        "Meeting status filter: ALL (default), IN_PROGRESS (started or "
                        "transcribing), STARTED, TRANSCRIBING, ERRORED or ENDED. Not case "
                        "sensitive."
                    ),
                },
                "limit": {
                    "type": "number",
                    "description": "Maximum number of meetings to return: a whole number from 1 to 1000 (default 20)",
                },
                "labels": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                    "description": (
                        "Only meetings that have every one of these labels, as an object of "
                        'key to value, for example {"project": "website-relaunch"}'
                    ),
                },
                "audience": {
                    "type": "string",
                    "enum": ["internal", "external"],
                    "description": "Only meetings with this audience: internal or external",
                },
                "updatedSince": {
                    "type": "string",
                    "description": "Only meetings changed at or after this ISO 8601 time",
                },
                "endedSince": {
                    "type": "string",
                    "description": (
                        "Only meetings that ended at or after this ISO 8601 time (an ended "
                        "meeting changed afterwards is listed too)"
                    ),
                },
            },
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
    },
    {
        "name": "schedule_meeting",
        "description": (
            "Schedule a future meeting with virtual participant. If you already have a "
            "Virtual Participant waiting for a meeting with the same name (ignoring case) "
            "and scheduled time (within a minute), that one is updated with the meeting "
            "link or ID, password, agenda, platform and meeting preparation given here "
            'instead of scheduling a second one, and the result has action "updated" and '
            'its virtualParticipantId (otherwise action "created"). A Virtual Participant '
            "cannot be changed within 5 minutes of its start. Pass allowDuplicate=true to "
            "schedule a second Virtual Participant anyway. The result's meetingId is the LMA "
            "meeting, null until the Virtual Participant has started it; platformMeetingId "
            "is the meeting ID or link given here. Change it later with "
            "update_scheduled_meeting."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "meetingName": {"type": "string", "description": "Name/title of the meeting"},
                "meetingPlatform": {
                    "type": "string",
                    "description": "Platform (Zoom, Teams, Chime, Webex, Google Meet)",
                },
                "meetingId": {
                    "type": "string",
                    "description": (
                        "Meeting ID (numeric ID only; for Google Meet the meeting code, e.g. "
                        "abc-defg-hij). For Zoom, the full join link from the invitation (for "
                        "example https://us05web.zoom.us/j/81234567890?pwd=...) may be passed "
                        "instead of the ID and passcode, and is the most reliable option; a "
                        "personal meeting link (https://<name>.zoom.us/my/<room>) works too."
                    ),
                },
                "scheduledTime": {"type": "string", "description": "ISO 8601 datetime"},
                "meetingPassword": {
                    "type": "string",
                    "description": (
                        "Optional meeting passcode, as the invitation text shows it (for "
                        'example "Passcode: 1234 5678"), not the pwd= value of a join link. '
                        "If only a Zoom link is available, pass the whole link as meetingId, "
                        "or the link's pwd value whole here."
                    ),
                },
                "agenda": {
                    "type": "string",
                    "description": (
                        "Optional meeting agenda, one item per line -- for example "
                        "the agenda from the calendar invite. It is stored on the "
                        "meeting record and shown on the meeting's page."
                    ),
                },
                "allowDuplicate": {
                    "type": "boolean",
                    "description": (
                        "Schedule a new Virtual Participant even if one is already "
                        "scheduled for a meeting with the same name and time (default "
                        "false: that one is updated instead)."
                    ),
                },
                "labels": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                    "description": "Optional labels for the meeting as an object of key to value, for example {\"project\": \"website-relaunch\"}, so it can be found by them later (list_meetings labels): at most 20; a key is 1 to 63 lower-case letters, digits, '.', '_' and '-', starting with a letter or digit; a value is at most 256 characters",
                },
                "audience": {
                    "type": "string",
                    "enum": ["internal", "external"],
                    "description": "Optional: internal (only the owner's organization) or external (customers or partners present)",
                },
                "context": {
                    "type": "string",
                    "description": "Optional meeting context: a Markdown briefing for the meeting assistant and the summary (goals, background, terms), at most 20000 characters",
                },
                "checklist": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "itemId": {
                                "type": "string",
                                "description": "A stable id you choose, unique in the meeting: 1 to 64 letters, digits, '.', '_', ':' and '-'",
                            },
                            "text": {
                                "type": "string",
                                "description": "What the item is, at most 500 characters",
                            },
                            "kind": {
                                "type": "string",
                                "enum": [
                                    "question",
                                    "topic",
                                    "risk",
                                    "confirm",
                                    "decision",
                                    "action",
                                ],
                                "description": "question, topic, risk, confirm, decision or action",
                            },
                            "priority": {
                                "type": "string",
                                "enum": ["must", "should", "could"],
                                "description": "must, should (the default) or could",
                            },
                            "source": {
                                "type": "string",
                                "description": "An opaque reference of yours, returned unchanged (at most 256 characters)",
                            },
                        },
                        "required": ["itemId", "text", "kind"],
                    },
                    "description": "Optional checklist: at most 100 items the meeting should cover; each starts open",
                },
                "shareWith": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional email addresses of LMA users to share the meeting with once the Virtual Participant creates it (at most 50); the Virtual Participant itself is not shared",
                },
            },
            "required": ["meetingName", "meetingPlatform", "meetingId", "scheduledTime"],
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False},
    },
    {
        "name": "start_meeting_now",
        "description": (
            "Start a meeting immediately with a virtual participant. "
            "When the user has stored Zoom credentials in LMA, the VP will "
            "sign in to Zoom with them by default for a more reliable "
            "join; pass useStoredZoomCredentials=false to force a guest "
            "join. Returns immediately with the virtualParticipantId; the "
            "VP may take up to 60 seconds to fully join. Use "
            "get_virtual_participant_status to poll progress, especially to "
            "detect MANUAL_ACTION_REQUIRED states (CAPTCHA, 2FA, SSO) where "
            "the human user must complete a challenge in the LMA viewer, and to "
            "read the meetingId (the LMA meeting) once the VP has started it."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "meetingName": {"type": "string", "description": "Name/title of the meeting"},
                "meetingPlatform": {
                    "type": "string",
                    "description": "Platform (Zoom, Teams, Chime, Webex, Google Meet)",
                },
                "meetingId": {
                    "type": "string",
                    "description": (
                        "Meeting ID (numeric ID only; for Google Meet the meeting code, e.g. "
                        "abc-defg-hij). For Zoom, the full join link from the invitation (for "
                        "example https://us05web.zoom.us/j/81234567890?pwd=...) may be passed "
                        "instead of the ID and passcode, and is the most reliable option; a "
                        "personal meeting link (https://<name>.zoom.us/my/<room>) works too."
                    ),
                },
                "meetingPassword": {
                    "type": "string",
                    "description": (
                        "Optional meeting passcode, as the invitation text shows it (for "
                        'example "Passcode: 1234 5678"), not the pwd= value of a join link. '
                        "If only a Zoom link is available, pass the whole link as meetingId, "
                        "or the link's pwd value whole here."
                    ),
                },
                "agenda": {
                    "type": "string",
                    "description": (
                        "Optional meeting agenda, one item per line -- for example "
                        "the agenda from the calendar invite. It is stored on the "
                        "meeting record and shown on the meeting's page."
                    ),
                },
                "useStoredZoomCredentials": {
                    "type": "boolean",
                    "description": (
                        "Default true. When the user has stored Zoom credentials, "
                        "the VP signs in to Zoom before joining the meeting. "
                        "Set to false to force a guest join."
                    ),
                },
                "labels": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                    "description": "Optional labels for the meeting as an object of key to value, for example {\"project\": \"website-relaunch\"}, so it can be found by them later (list_meetings labels): at most 20; a key is 1 to 63 lower-case letters, digits, '.', '_' and '-', starting with a letter or digit; a value is at most 256 characters",
                },
                "audience": {
                    "type": "string",
                    "enum": ["internal", "external"],
                    "description": "Optional: internal (only the owner's organization) or external (customers or partners present)",
                },
                "context": {
                    "type": "string",
                    "description": "Optional meeting context: a Markdown briefing for the meeting assistant and the summary (goals, background, terms), at most 20000 characters",
                },
                "checklist": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "itemId": {
                                "type": "string",
                                "description": "A stable id you choose, unique in the meeting: 1 to 64 letters, digits, '.', '_', ':' and '-'",
                            },
                            "text": {
                                "type": "string",
                                "description": "What the item is, at most 500 characters",
                            },
                            "kind": {
                                "type": "string",
                                "enum": [
                                    "question",
                                    "topic",
                                    "risk",
                                    "confirm",
                                    "decision",
                                    "action",
                                ],
                                "description": "question, topic, risk, confirm, decision or action",
                            },
                            "priority": {
                                "type": "string",
                                "enum": ["must", "should", "could"],
                                "description": "must, should (the default) or could",
                            },
                            "source": {
                                "type": "string",
                                "description": "An opaque reference of yours, returned unchanged (at most 256 characters)",
                            },
                        },
                        "required": ["itemId", "text", "kind"],
                    },
                    "description": "Optional checklist: at most 100 items the meeting should cover; each starts open",
                },
                "shareWith": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional email addresses of LMA users to share the meeting with once the Virtual Participant creates it (at most 50); the Virtual Participant itself is not shared",
                },
            },
            "required": ["meetingName", "meetingPlatform", "meetingId"],
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False},
    },
    {
        "name": "get_virtual_participant_status",
        "description": (
            "Get the current status of a virtual participant launched via "
            "start_meeting_now. Returns status, a human-readable summary, "
            "the meetingId of the LMA meeting once the VP has started it, "
            "and — critically — manualActionRequired=true with a "
            "manualActionMessage when the VP is blocked on a CAPTCHA / 2FA "
            "/ SSO / consent challenge that the human user must complete "
            "in the LMA viewer (virtualParticipantUrl). Poll this every "
            "5–10 seconds for ~2 minutes after start_meeting_now if "
            "you need to confirm the meeting was joined or surface a "
            "manual-action prompt back to your user."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "virtualParticipantId": {
                    "type": "string",
                    "description": "VP id returned by start_meeting_now",
                },
            },
            "required": ["virtualParticipantId"],
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
    },
]

# Every tool, on both front doors: the seven above and the meeting tools.
TOOLS: List[Dict[str, Any]] = [*MCP_TOOLS, *MEETING_TOOLS]


def is_read_only(tool_name: str) -> bool:
    """Whether a tool changes nothing: its annotations declare readOnlyHint."""
    tool = next((tool for tool in TOOLS if tool["name"] == tool_name), None)
    return bool(tool and (tool.get("annotations") or {}).get("readOnlyHint") is True)


ORIGINAL_TOOL_NAMES = frozenset(tool["name"] for tool in MCP_TOOLS)


def scope_allows(scope: str, tool_name: str) -> bool:
    """Whether a key with `scope` may call the tool."""
    if scope == READ_WRITE_SCOPE or is_read_only(tool_name):
        return True
    return scope == LEGACY_SCOPE and tool_name in ORIGINAL_TOOL_NAMES


def tools_for_scope(scope: str) -> list:
    """The tools a key with `scope` may call, which are the tools it is offered."""
    return [tool for tool in TOOLS if scope_allows(scope, tool["name"])]


def scope_refusal(tool_name: str, scope: str) -> str:
    """Why a key cannot call a tool that changes something."""
    held = (
        "this key was created before keys had scopes, and keeps the original tools "
        "and the read-only tools"
        if scope == LEGACY_SCOPE
        else "this key has the read scope"
    )
    return (
        f"{tool_name} changes LMA data, so it needs an MCP API key with the read-write "
        f"scope; {held}. Generate a read-write key on the MCP Servers page (Hosted MCP "
        "Access) to use it."
    )


class CallerInfo(NamedTuple):
    """Who a tool call is for: the user the request resolved to, and the key's scope."""

    user_id: str
    is_admin: bool
    scope: str


def _meeting(caller: CallerInfo) -> Any:
    """The caller as the meeting tools take it (tools/access.py)."""
    return access.Caller(user_id=caller.user_id, is_admin=caller.is_admin, scope=caller.scope)


# The arguments schedule_meeting and start_meeting_now gained with the meeting
# preparation tools. A legacy key keeps those two tools as they were, so it is
# refused these, as it is refused the tools that set them on a meeting.
PREPARATION_ARGUMENTS = ("labels", "audience", "context", "checklist", "shareWith")


def _vp_preparation(caller: CallerInfo, tool_name: str, a: Dict[str, Any]) -> Dict[str, Any]:
    """The meeting preparation arguments of schedule_meeting and start_meeting_now, checked."""
    given = [name for name in PREPARATION_ARGUMENTS if a.get(name) is not None]
    if given and caller.scope == LEGACY_SCOPE:
        raise PermissionError(
            f"{', '.join(given)} on {tool_name} change who can see the meeting and what it "
            "carries, so they need an MCP API key with the read-write scope; this key was "
            "created before keys had scopes, and keeps the original tools as they were. "
            "Generate a read-write key on the MCP Servers page (Hosted MCP Access) to use them."
        )
    return vp_preparation.vp_inputs(
        a.get("labels"), a.get("audience"), a.get("context"), a.get("checklist"), a.get("shareWith")
    )


# Every tool, called with the MCP caller and the tool's arguments.
TOOL_HANDLERS: Dict[str, Callable[[Any, Dict[str, Any]], Any]] = {
    "search_lma_meetings": lambda caller, a: search_meetings.execute(
        query=a.get("query"),
        start_date=a.get("startDate"),
        end_date=a.get("endDate"),
        max_results=a.get("maxResults", 10),
        user_id=caller.user_id,
        is_admin=caller.is_admin,
    ),
    "get_meeting_transcript": lambda caller, a: get_transcript.execute(
        meeting_id=a.get("meetingId"),
        format=a.get("format", "text"),
        user_id=caller.user_id,
        is_admin=caller.is_admin,
        next_token=a.get("nextToken"),
        max_segments=a.get("maxSegments"),
        include_assistant=a.get("includeAssistant") is True,
    ),
    "get_meeting_summary": lambda caller, a: get_summary.execute(
        meeting_id=a.get("meetingId"),
        include_action_items=a.get("includeActionItems", True),
        include_topics=a.get("includeTopics", True),
        user_id=caller.user_id,
        is_admin=caller.is_admin,
    ),
    "list_meetings": lambda caller, a: list_meetings.execute(
        start_date=a.get("startDate"),
        end_date=a.get("endDate"),
        participant=a.get("participant"),
        status=a.get("status", "ALL"),
        limit=a.get("limit", 20),
        user_id=caller.user_id,
        is_admin=caller.is_admin,
        labels=a.get("labels"),
        audience=a.get("audience"),
        updated_since=a.get("updatedSince"),
        ended_since=a.get("endedSince"),
    ),
    "schedule_meeting": lambda caller, a: schedule_meeting.execute(
        meeting_name=a.get("meetingName"),
        meeting_platform=a.get("meetingPlatform"),
        meeting_id=a.get("meetingId"),
        scheduled_time=a.get("scheduledTime"),
        meeting_password=a.get("meetingPassword"),
        user_id=caller.user_id,
        is_admin=caller.is_admin,
        agenda=a.get("agenda"),
        allow_duplicate=a.get("allowDuplicate") is True,
        preparation=_vp_preparation(caller, "schedule_meeting", a),
    ),
    "start_meeting_now": lambda caller, a: start_meeting_now.execute(
        meeting_name=a.get("meetingName"),
        meeting_platform=a.get("meetingPlatform"),
        meeting_id=a.get("meetingId"),
        meeting_password=a.get("meetingPassword"),
        user_id=caller.user_id,
        is_admin=caller.is_admin,
        # Default ON; pass useStoredZoomCredentials=false to opt-out.
        use_stored_zoom_credentials=a.get("useStoredZoomCredentials", True),
        agenda=a.get("agenda"),
        preparation=_vp_preparation(caller, "start_meeting_now", a),
    ),
    "get_virtual_participant_status": lambda caller, a: get_virtual_participant_status.execute(
        virtual_participant_id=a.get("virtualParticipantId"),
        user_id=caller.user_id,
        is_admin=caller.is_admin,
    ),
    "push_meeting_guidance": lambda caller, a: guidance.push(
        _meeting(caller),
        a.get("meeting_id"),
        a.get("text"),
        title=a.get("title"),
        level=a.get("level"),
    ),
    "list_meeting_guidance": lambda caller, a: guidance.list_items(
        _meeting(caller), a.get("meeting_id"), limit=a.get("limit")
    ),
    "get_meeting_agenda": lambda caller, a: meeting_agenda.get_agenda(
        _meeting(caller), a.get("meetingId")
    ),
    "update_meeting_agenda": lambda caller, a: meeting_agenda.update_agenda(
        _meeting(caller), a.get("meetingId"), a.get("agenda")
    ),
    "get_meeting_context": lambda caller, a: preparation.get_context(
        _meeting(caller), a.get("meetingId")
    ),
    "set_meeting_context": lambda caller, a: preparation.set_context(
        _meeting(caller), a.get("meetingId"), a.get("context")
    ),
    "get_meeting_checklist": lambda caller, a: preparation.get_checklist(
        _meeting(caller), a.get("meetingId")
    ),
    "set_meeting_checklist": lambda caller, a: preparation.set_checklist(
        _meeting(caller), a.get("meetingId"), a.get("items"), reset_statuses=a.get("resetStatuses")
    ),
    "update_checklist_item": lambda caller, a: preparation.update_item(
        _meeting(caller),
        a.get("meetingId"),
        a.get("itemId"),
        status=a.get("status"),
        note=a.get("note"),
    ),
    "update_meeting_metadata": lambda caller, a: preparation.update_metadata(
        _meeting(caller), a.get("meetingId"), labels=a.get("labels"), audience=a.get("audience")
    ),
    "get_meeting_consent": lambda caller, a: consent.execute(_meeting(caller), a.get("meetingId")),
    "update_scheduled_meeting": lambda caller, a: scheduled_meeting.execute(
        _meeting(caller),
        a.get("virtualParticipantId"),
        scheduled_time=a.get("scheduledTime"),
        meeting_name=a.get("meetingName"),
        agenda=a.get("agenda"),
        labels=a.get("labels"),
        audience=a.get("audience"),
        context=a.get("context"),
        checklist=a.get("checklist"),
        share_with=a.get("shareWith"),
    ),
    "end_meeting": lambda caller, a: meeting_actions.end_meeting(
        _meeting(caller), a.get("meetingId"), reason=a.get("reason")
    ),
    "end_virtual_participant": lambda caller, a: virtual_participants.end_by_id(
        _meeting(caller), a.get("virtualParticipantId"), reason=a.get("reason")
    ),
    "delete_meeting": lambda caller, a: meeting_actions.delete_meeting(
        _meeting(caller), a.get("meetingId"), confirm=a.get("confirm")
    ),
    "ask_meeting_assistant": lambda caller, a: ask_assistant.execute(
        _meeting(caller), a.get("meetingId"), a.get("question")
    ),
    "get_meeting_record": lambda caller, a: meeting_record.execute(
        _meeting(caller), a.get("meetingId"), include=a.get("include")
    ),
    "share_meeting": lambda caller, a: meeting_actions.share_meeting(
        _meeting(caller), a.get("meetingId"), a.get("users"), action=a.get("action")
    ),
    "regenerate_meeting_summary": lambda caller, a: meeting_actions.regenerate_summary(
        _meeting(caller), a.get("meetingId")
    ),
    "get_meeting_cost": lambda caller, a: meeting_actions.meeting_cost(
        _meeting(caller), a.get("meetingId")
    ),
    "list_virtual_participants": lambda caller, a: virtual_participants.list_vps(
        _meeting(caller), status=a.get("status"), limit=a.get("limit")
    ),
}


def handle_mcp_jsonrpc(msg, user_id, username, is_admin, scope=READ_SCOPE):
    """Handle MCP JSON-RPC 2.0 protocol messages."""
    method = msg.get("method")
    msg_id = msg.get("id")
    params = msg.get("params", {})

    logger.info(f"MCP JSON-RPC method={method}, id={msg_id}")

    if method == "initialize":
        return jsonrpc_response(
            msg_id,
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "lma-mcp-server", "version": server_version()},
            },
        )

    if method == "notifications/initialized":
        # Client acknowledgment — no response needed for notifications
        return {"statusCode": 200, "headers": {"Content-Type": "application/json"}, "body": ""}

    if method == "tools/list":
        return jsonrpc_response(msg_id, {"tools": tools_for_scope(scope)})

    if method == "tools/call":
        tool_name = params.get("name")
        arguments = params.get("arguments") or {}
        return execute_tool_call(msg_id, tool_name, arguments, user_id, username, is_admin, scope)

    if method == "ping":
        return jsonrpc_response(msg_id, {})

    return jsonrpc_error(msg_id, -32601, f"Method not found: {method}")


def execute_tool_call(  # pylint: disable=too-many-arguments
    msg_id, tool_name, arguments, user_id, username, is_admin, scope=READ_SCOPE
):
    """Execute a tool call and return MCP JSON-RPC response."""
    if tool_name not in TOOL_HANDLERS:
        return jsonrpc_error(msg_id, -32602, f"Unknown tool: {tool_name}")
    if not scope_allows(scope, tool_name):
        logger.warning(f"Refused {tool_name} for a key with the {scope} scope")
        return jsonrpc_response(
            msg_id,
            {
                "content": [{"type": "text", "text": scope_refusal(tool_name, scope)}],
                "isError": True,
            },
        )
    if not isinstance(arguments, dict):
        return jsonrpc_error(msg_id, -32602, "arguments must be an object")
    try:
        caller = CallerInfo(user_id, is_admin, scope)
        result = TOOL_HANDLERS[tool_name](caller, arguments)
        return jsonrpc_response(
            msg_id,
            {
                "content": [{"type": "text", "text": json.dumps(result, indent=2, default=str)}],
            },
        )
    except Exception as e:
        logger.error(f"Tool call error: {e}", exc_info=True)
        return jsonrpc_response(
            msg_id,
            {
                "content": [{"type": "text", "text": f"Error: {str(e)}"}],
                "isError": True,
            },
        )


def jsonrpc_response(msg_id, result):
    """Format a JSON-RPC 2.0 success response."""
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"jsonrpc": "2.0", "id": msg_id, "result": result}),
    }


def jsonrpc_error(msg_id, code, message):
    """Format a JSON-RPC 2.0 error response."""
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(
            {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}
        ),
    }


def success_response(result: Any) -> Dict[str, Any]:
    """Format successful MCP response"""
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(
            {"content": [{"type": "text", "text": json.dumps(result, indent=2, default=str)}]}
        ),
    }


def error_response(status_code: int, message: str) -> Dict[str, Any]:
    """Format error MCP response"""
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"error": {"message": message, "code": status_code}}),
    }
