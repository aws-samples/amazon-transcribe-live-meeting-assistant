# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""The MCP definitions of the meeting tools.

Both front doors serve them: the API key endpoint lists them from here, and the
OAuth endpoint (the AgentCore Gateway target) declares the same names,
descriptions and input schemas in the stack template, which
lma-ai-stack/test/test_mcp_tool_schemas.py holds equal to these.

A tool whose annotations carry ``readOnlyHint: true`` changes nothing and is
answered for any key. Every other tool here changes something and is answered
only for a key with the read-write scope (index.scope_allows); a key without it
is not offered them in tools/list either. ``destructiveHint`` marks the tools
whose change cannot be taken back or takes something away from people, and
``idempotentHint`` the ones that change nothing more when called again with
the same arguments.

The gateway's schema format has no ``enum`` and no ``additionalProperties``, so
each value an argument accepts is named in its description too.
"""

from typing import Any, Dict, List

READ_ONLY = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True}
WRITE = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False}
WRITE_IDEMPOTENT = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True}
DESTRUCTIVE = {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False}
DESTRUCTIVE_IDEMPOTENT = {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True}

MEETING_ID = {"type": "string", "description": "Meeting ID (CallId)"}
VP_ID = {"type": "string", "description": "Virtual Participant id"}

LABELS = {
    "type": "object",
    "additionalProperties": {"type": "string"},
    "description": (
        'Labels as an object of key to value, for example {"project": "website-relaunch"}: '
        "at most 20; a key is 1 to 63 lower-case letters, digits, '.', '_' and '-', starting "
        "with a letter or digit; a value is at most 256 characters"
    ),
}
AUDIENCE = {
    "type": "string",
    "enum": ["internal", "external", ""],
    "description": (
        "internal (only the owner's organization) or external (customers or partners "
        "present); an empty string clears it"
    ),
}
CONTEXT = {
    "type": "string",
    "description": (
        "The meeting context: a Markdown briefing for the meeting assistant and the "
        "summary (goals, background, terms), at most 20000 characters"
    ),
}
CHECKLIST_ITEM = {
    "type": "object",
    "properties": {
        "itemId": {
            "type": "string",
            "description": (
                "A stable id you choose, unique in the meeting: 1 to 64 letters, digits, "
                "'.', '_', ':' and '-'"
            ),
        },
        "text": {"type": "string", "description": "What the item is, at most 500 characters"},
        "kind": {
            "type": "string",
            "enum": ["question", "topic", "risk", "confirm", "decision", "action"],
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
}
CHECKLIST = {
    "type": "array",
    "items": CHECKLIST_ITEM,
    "description": "The checklist: at most 100 items the meeting should cover",
}
SHARE_WITH = {
    "type": "array",
    "items": {"type": "string"},
    "description": (
        "Email addresses of LMA users to share the meeting with once the Virtual "
        "Participant creates it (at most 50); the Virtual Participant itself is not shared"
    ),
}
AGENDA = {
    "type": "string",
    "description": "The agenda, one item per line (at most 10000 characters)",
}

MEETING_TOOLS: List[Dict[str, Any]] = [
    {
        "name": "push_meeting_guidance",
        "description": (
            "Leave a short note of guidance on a meeting for the people following it in "
            "LMA: it appears at once on the meeting page as a dismissible notification for "
            "the meeting's owner and the people it is shared with, and is stored with the "
            "meeting. It is NOT posted to the meeting's chat, so other participants do not "
            "see it. Requires a read-write API key."
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
        "description": "List the guidance notes pushed to a meeting, newest first.",
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
        "name": "get_meeting_agenda",
        "description": (
            "Get a meeting's agenda, with where it came from (agendaSource), who last "
            "changed it and when."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"meetingId": MEETING_ID},
            "required": ["meetingId"],
        },
        "annotations": READ_ONLY,
    },
    {
        "name": "update_meeting_agenda",
        "description": (
            "Replace a meeting's agenda, while it runs or after it has ended. The previous "
            "agenda is overwritten; pass an empty string to clear it. To change the agenda "
            "of a Virtual Participant that has not started yet, use update_scheduled_meeting. "
            "Requires a read-write API key."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"meetingId": MEETING_ID, "agenda": AGENDA},
            "required": ["meetingId", "agenda"],
        },
        "annotations": WRITE_IDEMPOTENT,
    },
    {
        "name": "get_meeting_context",
        "description": (
            "Get a meeting's context: the Markdown briefing for the meeting assistant and "
            "the summary, with who last changed it and when."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"meetingId": MEETING_ID},
            "required": ["meetingId"],
        },
        "annotations": READ_ONLY,
    },
    {
        "name": "set_meeting_context",
        "description": (
            "Replace a meeting's context (a Markdown briefing for the meeting assistant and "
            "the summary). Pass an empty string to clear it. Requires a read-write API key."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"meetingId": MEETING_ID, "context": CONTEXT},
            "required": ["meetingId", "context"],
        },
        "annotations": WRITE_IDEMPOTENT,
    },
    {
        "name": "get_meeting_checklist",
        "description": (
            "Get a meeting's checklist: each item with its status (open, covered, partial, "
            "deferred or not_applicable), who set it (statusSource user or assistant), its "
            "evidence in the transcript and its note."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"meetingId": MEETING_ID},
            "required": ["meetingId"],
        },
        "annotations": READ_ONLY,
    },
    {
        "name": "set_meeting_checklist",
        "description": (
            "Replace a meeting's whole checklist, in the order given. An item whose itemId "
            "was already there keeps its status, evidence and note, unless resetStatuses is "
            "true; an item left out is removed with its progress. Pass an empty list to clear "
            "it. Requires a read-write API key."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "meetingId": MEETING_ID,
                "items": CHECKLIST,
                "resetStatuses": {
                    "type": "boolean",
                    "description": "Start every item again as open (default false)",
                },
            },
            "required": ["meetingId", "items"],
        },
        "annotations": DESTRUCTIVE_IDEMPOTENT,
    },
    {
        "name": "update_checklist_item",
        "description": (
            "Set the status or the note of one checklist item. The status is recorded as "
            "set by a user (statusSource user), so the meeting assistant does not change it "
            "afterwards. Requires a read-write API key."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "meetingId": MEETING_ID,
                "itemId": {"type": "string", "description": "The item's itemId"},
                "status": {
                    "type": "string",
                    "enum": ["open", "covered", "partial", "deferred", "not_applicable"],
                    "description": "open, covered, partial, deferred or not_applicable",
                },
                "note": {
                    "type": "string",
                    "description": "A note, at most 1000 characters; an empty string clears it",
                },
            },
            "required": ["meetingId", "itemId"],
        },
        "annotations": WRITE_IDEMPOTENT,
    },
    {
        "name": "update_meeting_metadata",
        "description": (
            "Replace a meeting's labels, set its audience, or both. The labels given replace "
            "all of the meeting's labels; pass {} to clear them. Requires a read-write API key."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"meetingId": MEETING_ID, "labels": LABELS, "audience": AUDIENCE},
            "required": ["meetingId"],
        },
        "annotations": WRITE_IDEMPOTENT,
    },
    {
        "name": "get_meeting_consent",
        "description": (
            "Get a meeting's consent record: its mode (off, notify or opt-in), how consent "
            "is given in an opt-in meeting (each_participant or owner_attests), when the "
            "notice was posted, the owner's attestation, and one row per participant with "
            "their latest reply (consented, objected or no_reply), when and how it was given. "
            "Says whether evidence exists; the evidence images are not returned."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"meetingId": MEETING_ID},
            "required": ["meetingId"],
        },
        "annotations": READ_ONLY,
    },
    {
        "name": "update_scheduled_meeting",
        "description": (
            "Change a Virtual Participant that is waiting for its scheduled launch: its "
            "scheduled time, meeting name, agenda, labels, audience, context, checklist, or "
            "who the meeting is shared with once it starts. Each argument given replaces "
            "that setting; an empty value removes it. Refused within 5 minutes of the start, "
            "and a new time must be at least 5 minutes away. Only its owner or an Admin may "
            "change it. Requires a read-write API key."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "virtualParticipantId": VP_ID,
                "scheduledTime": {"type": "string", "description": "ISO 8601 datetime"},
                "meetingName": {"type": "string", "description": "Name/title of the meeting"},
                "agenda": AGENDA,
                "labels": LABELS,
                "audience": AUDIENCE,
                "context": CONTEXT,
                "checklist": CHECKLIST,
                "shareWith": SHARE_WITH,
            },
            "required": ["virtualParticipantId"],
        },
        "annotations": WRITE_IDEMPOTENT,
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
                "meetingId": MEETING_ID,
                "reason": {"type": "string", "description": "Optional reason, recorded on the VP"},
            },
            "required": ["meetingId"],
        },
        "annotations": DESTRUCTIVE,
    },
    {
        "name": "end_virtual_participant",
        "description": (
            "End a Virtual Participant: it leaves its meeting, which stops recording and "
            "transcription, or, if it is waiting for a scheduled launch, the launch is "
            "cancelled. This cannot be undone. Calling it again for one that has already "
            "finished changes nothing and says so (alreadyEnded). Only its owner or an Admin "
            "may end it. Requires a read-write API key."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "virtualParticipantId": VP_ID,
                "reason": {"type": "string", "description": "Optional reason, recorded on the VP"},
            },
            "required": ["virtualParticipantId"],
        },
        "annotations": DESTRUCTIVE_IDEMPOTENT,
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
                "meetingId": MEETING_ID,
                "confirm": {"type": "boolean", "description": "Must be true to delete"},
            },
            "required": ["meetingId", "confirm"],
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
                "meetingId": MEETING_ID,
                "question": {
                    "type": "string",
                    "description": "The question, at most 4000 characters",
                },
            },
            "required": ["meetingId", "question"],
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
                "meetingId": MEETING_ID,
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
                    "description": (
                        "Sections to return (default details and summary): details, summary, "
                        "transcript, chat_links, key_frames, chat_attachments, recordings, consent"
                    ),
                },
            },
            "required": ["meetingId"],
        },
        "annotations": READ_ONLY,
    },
    {
        "name": "share_meeting",
        "description": (
            "Change who a meeting is shared with (by email address): action add (the "
            "default) lets them read its transcript, summary and recordings in LMA; action "
            "remove takes that access away. Returns the meeting's sharedWith. Only the "
            "meeting's owner or an Admin may change it. Requires a read-write API key."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "meetingId": MEETING_ID,
                "users": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Email addresses (at most 50)",
                },
                "action": {
                    "type": "string",
                    "enum": ["add", "remove"],
                    "description": "add (default) or remove",
                },
            },
            "required": ["meetingId", "users"],
        },
        "annotations": DESTRUCTIVE_IDEMPOTENT,
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
            "properties": {"meetingId": MEETING_ID},
            "required": ["meetingId"],
        },
        "annotations": DESTRUCTIVE,
    },
    {
        "name": "get_meeting_cost",
        "description": "Get the itemised cost estimate of a meeting (AWS services it used).",
        "inputSchema": {
            "type": "object",
            "properties": {"meetingId": MEETING_ID},
            "required": ["meetingId"],
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
]
