# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""Tools: a meeting's whiteboards.

* list_meeting_whiteboards / get_meeting_whiteboard (any key)
* create_meeting_whiteboard / update_meeting_whiteboard (a read-write key)

Each reads the meeting's row first (tools/access.py), so it answers the callers
getCall answers: an Admin, the meeting's owner, or a user it is shared with. The
reads and writes are the operations the meeting page uses
(listMeetingWhiteboards, getMeetingWhiteboard, createMeetingWhiteboard,
updateMeetingWhiteboard), answered by the meeting whiteboards resolver, which
holds the same rule. This function's IAM role is a backend caller there, so a
write names who it is for: ``Source`` is ``user`` and ``UpdatedBy`` the MCP
caller, because an MCP client acts for its user. A panel it changes is then the
user's, and the assistant does not change it afterwards.

``get_meeting_whiteboard`` takes ``ifNewerThan``, the version a client already
has, and answers ``{notModified: true}`` while the whiteboard is still at it, so
a client can poll cheaply. The arguments and answers are camelCase, and are
checked with the rules the resolver applies (lma_whiteboards) before anything
is sent. See docs/meeting-whiteboards.md.
"""

from typing import Any, Dict, List, Mapping, Optional

from lma_whiteboards import rules  # pylint: disable=import-error

from tools.access import Caller, require_meeting
from tools.appsync_client import graphql

_FIELDS = """
    CallId WhiteboardId Title Template Instructions AssistantUpdates Version
    CreatedBy CreatedAt UpdatedBy UpdatedAt Owner SharedWith
    Panels { PanelId Title Kind Content Source UpdatedBy UpdatedAt }
"""

LIST = (
    "query ListMeetingWhiteboards($CallId: ID!) {\n"
    "  listMeetingWhiteboards(CallId: $CallId) {\n"
    "    CallId WhiteboardsEnabled AssistantAvailable MaxWhiteboards Writable\n"
    "    Items {" + _FIELDS + "}\n  }\n}"
)
GET = (
    "query GetMeetingWhiteboard($CallId: ID!, $WhiteboardId: ID!, $Version: Int) {\n"
    "  getMeetingWhiteboard(CallId: $CallId, WhiteboardId: $WhiteboardId, Version: $Version) {"
    + _FIELDS
    + "}\n}"
)
CREATE = (
    "mutation CreateMeetingWhiteboard($input: CreateMeetingWhiteboardInput!) {\n"
    "  createMeetingWhiteboard(input: $input) {" + _FIELDS + "}\n}"
)
UPDATE = (
    "mutation UpdateMeetingWhiteboard($input: UpdateMeetingWhiteboardInput!) {\n"
    "  updateMeetingWhiteboard(input: $input) {" + _FIELDS + "}\n}"
)

FORMATS = ("json", "markdown")


def _checked(check, *args, **kwargs):
    """Run one of the rules, reporting a broken one as the tools' ValueError."""
    try:
        return check(*args, **kwargs)
    except rules.WhiteboardError as error:
        raise ValueError(str(error)) from error


def _call_id(meeting: Mapping[str, Any], meeting_id: Any) -> str:
    return meeting.get("CallId") or str(meeting_id).strip()


def _whiteboard_id(value: Any) -> str:
    return _checked(rules.check_id, value, "whiteboardId")


def panels_input(value: Any) -> List[Dict[str, Any]]:
    """MCP panels ({panelId, title, kind, content, source}) as the resolver takes them."""
    if not isinstance(value, list):
        raise ValueError("panels must be a list")
    panels = []
    for item in value:
        if not isinstance(item, Mapping):
            raise ValueError("each panel must be an object")
        panel = {
            "PanelId": item.get("panelId") or None,
            "Title": item.get("title"),
            # Absent, the resolver keeps an existing panel's kind (Markdown for a new one).
            "Kind": item.get("kind") or None,
            # Absent, the resolver keeps an existing panel's content.
            "Content": item.get("content") if isinstance(item.get("content"), str) else None,
        }
        if item.get("source"):
            panel["Source"] = item.get("source")
        panels.append(panel)
    _checked(rules.panels_input, panels)
    return [{k: v for k, v in p.items() if v is not None} for p in panels]


def _output(
    board: Optional[Mapping[str, Any]], meeting_id: str, fmt: str = "json"
) -> Dict[str, Any]:
    if not board:
        raise ValueError("There is no such whiteboard on this meeting")
    out = {"meetingId": meeting_id, **rules.whiteboard_output(board)}
    if fmt == "markdown":
        out["markdown"] = rules.whiteboard_markdown(board)
        out.pop("panels", None)
    return out


def list_whiteboards(caller: Caller, meeting_id: Any) -> Dict[str, Any]:
    """list_meeting_whiteboards: the whiteboards without their panels."""
    meeting = require_meeting(meeting_id, caller)
    call_id = _call_id(meeting, meeting_id)
    listed = graphql(LIST, {"CallId": call_id}).get("listMeetingWhiteboards") or {}
    return {
        "meetingId": call_id,
        "assistantUpdates": listed.get("WhiteboardsEnabled") is True,
        "assistantAvailable": listed.get("AssistantAvailable") is not False,
        "maxWhiteboards": listed.get("MaxWhiteboards"),
        "writable": listed.get("Writable") is not False,
        "whiteboards": [
            rules.whiteboard_output(b, panels=False) for b in listed.get("Items") or []
        ],
    }


def get_whiteboard(  # pylint: disable=too-many-arguments
    caller: Caller,
    meeting_id: Any,
    whiteboard_id: Any,
    version: Any = None,
    if_newer_than: Any = None,
    fmt: Any = None,
) -> Dict[str, Any]:
    """get_meeting_whiteboard: one whiteboard, or one earlier version of it."""
    chosen = (fmt or "json").strip().lower() if isinstance(fmt, str) or fmt is None else ""
    if chosen not in FORMATS:
        raise ValueError(f"format must be one of: {', '.join(FORMATS)}")
    meeting = require_meeting(meeting_id, caller)
    call_id = _call_id(meeting, meeting_id)
    variables: Dict[str, Any] = {"CallId": call_id, "WhiteboardId": _whiteboard_id(whiteboard_id)}
    if version is not None:
        variables["Version"] = _integer(version, "version")
    board = graphql(GET, variables).get("getMeetingWhiteboard")
    if if_newer_than is not None and board and version is None:
        known = _integer(if_newer_than, "ifNewerThan")
        if int(board.get("Version") or 0) <= known:
            return {
                "meetingId": call_id,
                "whiteboardId": board.get("WhiteboardId"),
                "version": board.get("Version"),
                "notModified": True,
            }
    return _output(board, call_id, chosen)


def _integer(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a whole number")
    try:
        return int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be a whole number") from error


def create_whiteboard(caller: Caller, meeting_id: Any, args: Mapping[str, Any]) -> Dict[str, Any]:
    """create_meeting_whiteboard."""
    template = _checked(rules.check_template, args.get("template"))
    inputs: Dict[str, Any] = {
        "Template": template,
        "Source": rules.SOURCE_USER,
        "UpdatedBy": caller.user_id,
    }
    for name, field in (("title", "Title"), ("instructions", "Instructions")):
        if args.get(name) is not None:
            inputs[field] = args.get(name)
    if args.get("assistantUpdates") is not None:
        inputs["AssistantUpdates"] = args.get("assistantUpdates") is True
    if args.get("panels"):
        inputs["Panels"] = panels_input(args.get("panels"))
    meeting = require_meeting(meeting_id, caller)
    call_id = _call_id(meeting, meeting_id)
    created = graphql(CREATE, {"input": {"CallId": call_id, **inputs}}).get(
        "createMeetingWhiteboard"
    )
    return _output(created, call_id)


def update_whiteboard(caller: Caller, meeting_id: Any, args: Mapping[str, Any]) -> Dict[str, Any]:
    """update_meeting_whiteboard: refused when the whiteboard is no longer at baseVersion."""
    inputs: Dict[str, Any] = {
        "WhiteboardId": _whiteboard_id(args.get("whiteboardId")),
        "BaseVersion": _integer(args.get("baseVersion"), "baseVersion"),
        "Source": rules.SOURCE_USER,
        "UpdatedBy": caller.user_id,
    }
    for name, field in (("title", "Title"), ("instructions", "Instructions")):
        if args.get(name) is not None:
            inputs[field] = args.get(name)
    if args.get("assistantUpdates") is not None:
        inputs["AssistantUpdates"] = args.get("assistantUpdates") is True
    if args.get("panels") is not None:
        inputs["Panels"] = panels_input(args.get("panels"))
    if len(inputs) == 4:
        raise ValueError("Give at least one of title, instructions, assistantUpdates or panels")
    meeting = require_meeting(meeting_id, caller)
    call_id = _call_id(meeting, meeting_id)
    updated = graphql(UPDATE, {"input": {"CallId": call_id, **inputs}}).get(
        "updateMeetingWhiteboard"
    )
    return _output(updated, call_id)
