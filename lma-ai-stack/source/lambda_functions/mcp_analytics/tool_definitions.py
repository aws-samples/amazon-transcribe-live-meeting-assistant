# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""The MCP definitions of the meeting tools served on the API key endpoint.

A tool whose annotations carry ``readOnlyHint: true`` changes nothing and is
answered for any key. Every other tool here changes something and is answered
only for a key with the read-write scope (index.scope_allows); a key without it
is not offered them in tools/list either. ``destructiveHint`` marks the
tools whose change cannot be taken back or takes something away from people.
"""

from typing import Any, Dict, List

READ_ONLY = {"readOnlyHint": True}
WRITE = {"readOnlyHint": False, "destructiveHint": False}
DESTRUCTIVE = {"readOnlyHint": False, "destructiveHint": True}

MEETING_ID = {"type": "string", "description": "Meeting ID (CallId)"}
VP_ID = {"type": "string", "description": "Virtual Participant id"}

MEETING_TOOLS: List[Dict[str, Any]] = [
    {
        "name": "get_live_transcript",
        "description": (
            "Read a meeting's transcript a little at a time while it runs, by polling. "
            "Returns the speech lines and meeting-chat lines added since the cursor "
            "passed as `since`, a new `cursor` for the next call, and `live` (whether the "
            "meeting is still being captured). Call it first without `since`, then every "
            "`pollAfterSeconds` (about 5 seconds) with the cursor from the previous answer; "
            "when `hasMore` is true, call again at once. Stop once `live` is false and a "
            "call returns no lines. Works on ended meetings too."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "meeting_id": MEETING_ID,
                "since": {
                    "type": "string",
                    "description": "The cursor from the previous answer; omit to start at the beginning",
                },
                "limit": {
                    "type": "number",
                    "description": "Most lines to return (default 200, max 1000)",
                },
                "include_assistant": {
                    "type": "boolean",
                    "description": "Also return the meeting assistant's answers (default false)",
                },
            },
            "required": ["meeting_id"],
        },
        "annotations": READ_ONLY,
    },
    {
        "name": "push_meeting_guidance",
        "description": (
            "Leave a short note of guidance on a meeting for the people following it in "
            "LMA: it appears at once in the meeting page's notifications panel for the "
            "meeting's owner and the people it is shared with, and is stored with the "
            "meeting until its transcript is deleted. It is NOT posted to the meeting's "
            "chat, so other participants do not see it. Requires a read-write API key."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "meeting_id": MEETING_ID,
                "text": {"type": "string", "description": "The guidance, at most 2000 characters"},
                "title": {
                    "type": "string",
                    "description": "Optional heading, at most 200 characters",
                },
                "level": {
                    "type": "string",
                    "enum": ["info", "warning", "urgent"],
                    "description": "How prominently it is shown (default info)",
                },
            },
            "required": ["meeting_id", "text"],
        },
        "annotations": WRITE,
    },
    {
        "name": "list_meeting_guidance",
        "description": "List the guidance given on a meeting, newest first.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "meeting_id": MEETING_ID,
                "limit": {"type": "number", "description": "Most items to return (default 50)"},
            },
            "required": ["meeting_id"],
        },
        "annotations": READ_ONLY,
    },
    {
        "name": "list_meeting_notifications",
        "description": (
            "List the notifications LMA showed on a meeting's page to the people who can see "
            "the meeting -- guidance (category 'guidance') and any other category -- oldest "
            "first, a page at a time. Each has its category, type, severity, title, body "
            "(Markdown), refs, dedupKey, actions and times. Pass since (ISO 8601) to get only "
            "those created or updated since then, and nextCursor as cursor for the next page."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "meeting_id": MEETING_ID,
                "category": {"type": "string", "description": "Only this category"},
                "since": {
                    "type": "string",
                    "description": "Only those created or updated at or after this ISO 8601 time",
                },
                "limit": {"type": "number", "description": "Most items to return (default 100)"},
                "cursor": {"type": "string", "description": "nextCursor from the previous page"},
            },
            "required": ["meeting_id"],
        },
        "annotations": READ_ONLY,
    },
    {
        "name": "update_meeting_agenda",
        "description": (
            "Replace the agenda of a meeting in progress (meeting_id) or of a Virtual "
            "Participant waiting for its scheduled launch (virtual_participant_id, at least "
            "5 minutes before it starts). The previous agenda is overwritten; pass an empty "
            "string to clear it. Requires a read-write API key."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "meeting_id": MEETING_ID,
                "virtual_participant_id": VP_ID,
                "agenda": {
                    "type": "string",
                    "description": "The agenda, one item per line (at most 10000 characters)",
                },
            },
            "required": ["agenda"],
        },
        "annotations": WRITE,
    },
    {
        "name": "end_meeting",
        "description": (
            "End a meeting that a Virtual Participant is capturing: the VP leaves the "
            "meeting and recording and transcription stop, which cannot be undone. A "
            "meeting streamed from a browser tab or the desktop app is ended where it is "
            "streamed instead. Only the VP's owner or an Admin may end it. Requires a "
            "read-write API key."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "meeting_id": MEETING_ID,
                "reason": {"type": "string", "description": "Optional reason, recorded on the VP"},
            },
            "required": ["meeting_id"],
        },
        "annotations": DESTRUCTIVE,
    },
    {
        "name": "delete_meeting",
        "description": (
            "PERMANENTLY DELETE a meeting and everything stored with it: transcript, "
            "summary, recordings, key frames, chat attachments, consent record. This cannot "
            "be undone. Nothing is deleted unless `confirm` is true; confirm with the user "
            "first. Only the meeting's owner or an Admin may delete it. Requires a "
            "read-write API key."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "meeting_id": MEETING_ID,
                "confirm": {"type": "boolean", "description": "Must be true to delete"},
            },
            "required": ["meeting_id", "confirm"],
        },
        "annotations": DESTRUCTIVE,
    },
    {
        "name": "ask_meeting_assistant",
        "description": (
            "Ask the LMA meeting assistant a question about one meeting, the way the "
            "meeting page's assistant chat does, and get its answer. The answer is also "
            "added to the meeting's assistant conversation on the meeting page. Uses the "
            "meeting assistant's model and tools, so it can take up to a minute. Requires "
            "a read-write API key."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "meeting_id": MEETING_ID,
                "question": {
                    "type": "string",
                    "description": "The question, at most 4000 characters",
                },
            },
            "required": ["meeting_id", "question"],
        },
        "annotations": WRITE,
    },
    {
        "name": "get_meeting_record",
        "description": (
            "Get the parts of a meeting's record named in `include`: details, summary, "
            "transcript, chat_links, key_frames, chat_attachments (titles, summaries and "
            "download links), recordings (audio and video download links) and consent (the "
            "consent record and its evidence screenshots; meeting owner or Admin only). "
            "Defaults to details and summary. The answer contains SENSITIVE content and "
            "pre-signed download links that work for anyone holding them until "
            "`linksExpireAt` (one hour); do not share them."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "meeting_id": MEETING_ID,
                "include": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": [
                            "details",
                            "summary",
                            "transcript",
                            "chat_links",
                            "key_frames",
                            "chat_attachments",
                            "recordings",
                            "consent",
                        ],
                    },
                    "description": "Sections to return (default details and summary)",
                },
            },
            "required": ["meeting_id"],
        },
        "annotations": READ_ONLY,
    },
    {
        "name": "share_meeting",
        "description": (
            "Share a meeting with more people (by email address): they can then read its "
            "transcript, summary and recordings in LMA. Only the meeting's owner or an Admin "
            "may share it. Requires a read-write API key."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "meeting_id": MEETING_ID,
                "recipients": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Email addresses to add",
                },
            },
            "required": ["meeting_id", "recipients"],
        },
        "annotations": WRITE,
    },
    {
        "name": "unshare_meeting",
        "description": (
            "Stop sharing a meeting with the given people (by email address); they lose "
            "access to it in LMA. Only the meeting's owner or an Admin may change it. "
            "Requires a read-write API key."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "meeting_id": MEETING_ID,
                "recipients": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Email addresses to remove",
                },
            },
            "required": ["meeting_id", "recipients"],
        },
        "annotations": DESTRUCTIVE,
    },
    {
        "name": "regenerate_meeting_summary",
        "description": (
            "Generate the meeting's summary again from its transcript. The new summary "
            "REPLACES the current one, including any edits made to it, when it finishes "
            "(usually within a few minutes). Requires a read-write API key."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"meeting_id": MEETING_ID},
            "required": ["meeting_id"],
        },
        "annotations": DESTRUCTIVE,
    },
    {
        "name": "get_meeting_cost",
        "description": "Get the itemised cost estimate of a meeting (AWS services it used).",
        "inputSchema": {
            "type": "object",
            "properties": {"meeting_id": MEETING_ID},
            "required": ["meeting_id"],
        },
        "annotations": READ_ONLY,
    },
    {
        "name": "list_virtual_participants",
        "description": (
            "List the Virtual Participants you can see (yours and those shared with you), "
            "newest first, with their status and meeting. Start one with start_meeting_now "
            "or schedule_meeting."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "description": "Only this status, e.g. ACTIVE or SCHEDULED",
                },
                "limit": {"type": "number", "description": "Most to return (default 25, max 100)"},
            },
        },
        "annotations": READ_ONLY,
    },
    {
        "name": "end_virtual_participant",
        "description": (
            "End a Virtual Participant: it leaves its meeting, which stops recording and "
            "transcription, or, if it is waiting for a scheduled launch, the launch is "
            "cancelled. This cannot be undone. Only its owner or an Admin may end it. "
            "Requires a read-write API key."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "virtual_participant_id": VP_ID,
                "reason": {"type": "string", "description": "Optional reason, recorded on the VP"},
            },
            "required": ["virtual_participant_id"],
        },
        "annotations": DESTRUCTIVE,
    },
]
