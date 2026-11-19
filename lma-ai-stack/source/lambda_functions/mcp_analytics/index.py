#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""
MCP Analytics Lambda Function
Implements MCP protocol tools for LMA meeting data access.

The BedrockAgentCore Gateway path serves the seven original tools, inferred from
their arguments. The API key path speaks MCP JSON-RPC and serves those and the
meeting tools in tool_definitions.py. On that path every API key carries a
scope (execute_tool_call): a read key is answered only by the tools whose
annotations declare readOnlyHint, a read-write key by every tool, and a legacy
key -- one stored before keys had scopes -- by the seven original tools and the
read-only meeting tools. Access to each meeting is decided in tools/access.py.
"""

import json
import logging
import os
from typing import Any, Dict, List

from tool_definitions import MEETING_TOOLS

# Import tool implementations
from tools import (
    access,
    ask_assistant,
    get_summary,
    get_transcript,
    get_virtual_participant_status,
    guidance,
    list_meetings,
    live_transcript,
    meeting_actions,
    meeting_agenda,
    meeting_record,
    schedule_meeting,
    search_meetings,
    start_meeting_now,
    virtual_participants,
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
            # The gateway authenticates the user with OAuth; no API key, so no scope.
            key_scope_to_apply = None

        logger.info(f"User: {username}, ID: {user_id}, Admin: {is_admin}")

        # Determine which tool based on input parameters
        if "virtualParticipantId" in tool_input and len(tool_input) <= 2:
            tool_name = "get_virtual_participant_status"
        elif "query" in tool_input and "maxResults" in tool_input:
            tool_name = "search_lma_meetings"
        elif "meetingId" in tool_input and "format" in tool_input:
            tool_name = "get_meeting_transcript"
        elif "meetingId" in tool_input and (
            "includeActionItems" in tool_input or "includeTopics" in tool_input
        ):
            tool_name = "get_meeting_summary"
        elif "meetingPlatform" in tool_input and "scheduledTime" in tool_input:
            tool_name = "schedule_meeting"
        elif (
            "meetingPlatform" in tool_input
            and "meetingName" in tool_input
            and "scheduledTime" not in tool_input
        ):
            tool_name = "start_meeting_now"
        elif "limit" in tool_input or "status" in tool_input or "participant" in tool_input:
            tool_name = "list_meetings"
        else:
            # Default to list_meetings if we can't determine
            tool_name = "list_meetings"

        # On the gateway path `tool_input` is the event itself, which carries the
        # caller's claims, so the argument names are logged rather than the values.
        logger.info(f"Inferred tool: {tool_name}, Input keys: {sorted(tool_input.keys())}")

        # A request on the API key endpoint is held to its key's scope whatever
        # the shape of its body, as execute_tool_call holds a JSON-RPC call.
        if key_scope_to_apply is not None and not scope_allows(key_scope_to_apply, tool_name):
            logger.warning(f"Refused {tool_name} for a key with the {key_scope_to_apply} scope")
            return error_response(403, scope_refusal(tool_name, key_scope_to_apply))

        # Route to appropriate tool
        if tool_name == "search_lma_meetings":
            result = search_meetings.execute(
                query=tool_input.get("query"),
                start_date=tool_input.get("startDate"),
                end_date=tool_input.get("endDate"),
                max_results=tool_input.get("maxResults", 10),
                user_id=user_id,
                is_admin=is_admin,
            )

        elif tool_name == "get_meeting_transcript":
            result = get_transcript.execute(
                meeting_id=tool_input.get("meetingId"),
                format=tool_input.get("format", "text"),
                user_id=user_id,
                is_admin=is_admin,
            )

        elif tool_name == "get_meeting_summary":
            result = get_summary.execute(
                meeting_id=tool_input.get("meetingId"),
                include_action_items=tool_input.get("includeActionItems", True),
                include_topics=tool_input.get("includeTopics", True),
                user_id=user_id,
                is_admin=is_admin,
            )

        elif tool_name == "list_meetings":
            result = list_meetings.execute(
                start_date=tool_input.get("startDate"),
                end_date=tool_input.get("endDate"),
                participant=tool_input.get("participant"),
                status=tool_input.get("status", "ALL"),
                limit=tool_input.get("limit", 20),
                user_id=user_id,
                is_admin=is_admin,
            )

        elif tool_name == "schedule_meeting":
            result = schedule_meeting.execute(
                meeting_name=tool_input.get("meetingName"),
                meeting_platform=tool_input.get("meetingPlatform"),
                meeting_id=tool_input.get("meetingId"),
                scheduled_time=tool_input.get("scheduledTime"),
                meeting_password=tool_input.get("meetingPassword"),
                user_id=user_id,
                is_admin=is_admin,
                agenda=tool_input.get("agenda"),
                allow_duplicate=tool_input.get("allowDuplicate") is True,
            )

        elif tool_name == "start_meeting_now":
            result = start_meeting_now.execute(
                meeting_name=tool_input.get("meetingName"),
                meeting_platform=tool_input.get("meetingPlatform"),
                meeting_id=tool_input.get("meetingId"),
                meeting_password=tool_input.get("meetingPassword"),
                user_id=user_id,
                is_admin=is_admin,
                # Default ON; pass useStoredZoomCredentials=false to opt-out.
                use_stored_zoom_credentials=tool_input.get("useStoredZoomCredentials", True),
                agenda=tool_input.get("agenda"),
            )

        elif tool_name == "get_virtual_participant_status":
            result = get_virtual_participant_status.execute(
                virtual_participant_id=tool_input.get("virtualParticipantId"),
                user_id=user_id,
                is_admin=is_admin,
            )

        else:
            return error_response(400, f"Unknown tool: {tool_name}")

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
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "get_meeting_transcript",
        "description": "Retrieve the complete transcript for a specific meeting",
        "inputSchema": {
            "type": "object",
            "properties": {
                "meetingId": {"type": "string", "description": "Meeting ID (CallId)"},
                "format": {"type": "string", "description": "Output format (json, text, or srt)"},
            },
            "required": ["meetingId"],
        },
        "annotations": {"readOnlyHint": True},
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
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "list_meetings",
        "description": "List meetings with optional filters",
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
            },
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "schedule_meeting",
        "description": (
            "Schedule a future meeting with virtual participant. If you already have a "
            "Virtual Participant waiting for a meeting with the same name (ignoring case) "
            "and scheduled time (within a minute), that one is updated with the meeting "
            "link or ID, password, agenda and platform given here instead of scheduling a "
            'second one, and the result has action "updated" and its '
            'virtualParticipantId (otherwise action "created"). A Virtual Participant '
            "cannot be changed within 5 minutes of its start. Pass allowDuplicate=true to "
            "schedule a second Virtual Participant anyway."
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
            },
            "required": ["meetingName", "meetingPlatform", "meetingId", "scheduledTime"],
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False},
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
            "the human user must complete a challenge in the LMA viewer."
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
            },
            "required": ["meetingName", "meetingPlatform", "meetingId"],
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False},
    },
    {
        "name": "get_virtual_participant_status",
        "description": (
            "Get the current status of a virtual participant launched via "
            "start_meeting_now. Returns status, a human-readable summary, "
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
        "annotations": {"readOnlyHint": True},
    },
]

# Every tool the API key endpoint serves: the seven above, which the gateway
# path serves too, and the meeting tools.
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


# The meeting tools, called with the MCP caller and the tool's arguments.
MEETING_TOOL_HANDLERS = {
    "get_live_transcript": lambda caller, a: live_transcript.execute(
        caller,
        a.get("meeting_id"),
        since=a.get("since"),
        limit=a.get("limit"),
        include_assistant=a.get("include_assistant") is True,
    ),
    "push_meeting_guidance": lambda caller, a: guidance.push(
        caller, a.get("meeting_id"), a.get("text"), title=a.get("title"), level=a.get("level")
    ),
    "list_meeting_guidance": lambda caller, a: guidance.list_items(
        caller, a.get("meeting_id"), limit=a.get("limit")
    ),
    "update_meeting_agenda": lambda caller, a: meeting_agenda.execute(
        caller,
        a.get("agenda"),
        meeting_id=a.get("meeting_id"),
        virtual_participant_id=a.get("virtual_participant_id"),
    ),
    "end_meeting": lambda caller, a: meeting_actions.end_meeting(
        caller, a.get("meeting_id"), reason=a.get("reason")
    ),
    "delete_meeting": lambda caller, a: meeting_actions.delete_meeting(
        caller, a.get("meeting_id"), confirm=a.get("confirm")
    ),
    "ask_meeting_assistant": lambda caller, a: ask_assistant.execute(
        caller, a.get("meeting_id"), a.get("question")
    ),
    "get_meeting_record": lambda caller, a: meeting_record.execute(
        caller, a.get("meeting_id"), include=a.get("include")
    ),
    "share_meeting": lambda caller, a: meeting_actions.change_sharing(
        caller, a.get("meeting_id"), a.get("recipients"), share=True
    ),
    "unshare_meeting": lambda caller, a: meeting_actions.change_sharing(
        caller, a.get("meeting_id"), a.get("recipients"), share=False
    ),
    "regenerate_meeting_summary": lambda caller, a: meeting_actions.regenerate_summary(
        caller, a.get("meeting_id")
    ),
    "get_meeting_cost": lambda caller, a: meeting_actions.meeting_cost(caller, a.get("meeting_id")),
    "list_virtual_participants": lambda caller, a: virtual_participants.list_vps(
        caller, status=a.get("status"), limit=a.get("limit")
    ),
    "end_virtual_participant": lambda caller, a: virtual_participants.end_by_id(
        caller, a.get("virtual_participant_id"), reason=a.get("reason")
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
                "serverInfo": {"name": "lma-mcp-server", "version": "0.2.30"},
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
    known = any(tool["name"] == tool_name for tool in TOOLS)
    if known and not scope_allows(scope, tool_name):
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
        if tool_name in MEETING_TOOL_HANDLERS:
            caller = access.Caller(user_id=user_id, is_admin=is_admin, scope=scope)
            result = MEETING_TOOL_HANDLERS[tool_name](caller, arguments)
        elif tool_name == "search_lma_meetings":
            result = search_meetings.execute(
                query=arguments.get("query"),
                start_date=arguments.get("startDate"),
                end_date=arguments.get("endDate"),
                max_results=arguments.get("maxResults", 10),
                user_id=user_id,
                is_admin=is_admin,
            )
        elif tool_name == "get_meeting_transcript":
            result = get_transcript.execute(
                meeting_id=arguments.get("meetingId"),
                format=arguments.get("format", "text"),
                user_id=user_id,
                is_admin=is_admin,
            )
        elif tool_name == "get_meeting_summary":
            result = get_summary.execute(
                meeting_id=arguments.get("meetingId"),
                include_action_items=arguments.get("includeActionItems", True),
                include_topics=arguments.get("includeTopics", True),
                user_id=user_id,
                is_admin=is_admin,
            )
        elif tool_name == "list_meetings":
            result = list_meetings.execute(
                start_date=arguments.get("startDate"),
                end_date=arguments.get("endDate"),
                participant=arguments.get("participant"),
                status=arguments.get("status", "ALL"),
                limit=arguments.get("limit", 20),
                user_id=user_id,
                is_admin=is_admin,
            )
        elif tool_name == "schedule_meeting":
            result = schedule_meeting.execute(
                meeting_name=arguments.get("meetingName"),
                meeting_platform=arguments.get("meetingPlatform"),
                meeting_id=arguments.get("meetingId"),
                scheduled_time=arguments.get("scheduledTime"),
                meeting_password=arguments.get("meetingPassword"),
                user_id=user_id,
                is_admin=is_admin,
                agenda=arguments.get("agenda"),
                allow_duplicate=arguments.get("allowDuplicate") is True,
            )
        elif tool_name == "start_meeting_now":
            result = start_meeting_now.execute(
                meeting_name=arguments.get("meetingName"),
                meeting_platform=arguments.get("meetingPlatform"),
                meeting_id=arguments.get("meetingId"),
                meeting_password=arguments.get("meetingPassword"),
                user_id=user_id,
                is_admin=is_admin,
                use_stored_zoom_credentials=arguments.get("useStoredZoomCredentials", True),
                agenda=arguments.get("agenda"),
            )
        elif tool_name == "get_virtual_participant_status":
            result = get_virtual_participant_status.execute(
                virtual_participant_id=arguments.get("virtualParticipantId"),
                user_id=user_id,
                is_admin=is_admin,
            )
        else:
            return jsonrpc_error(msg_id, -32602, f"Unknown tool: {tool_name}")

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
