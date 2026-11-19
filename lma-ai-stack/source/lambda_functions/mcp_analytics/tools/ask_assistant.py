# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""Tool: ask_meeting_assistant -- a question to the meeting assistant, about one meeting.

Asked the way the meeting page's assistant chat asks it: the same event
strands_chat_interface builds for sendChatMessage, on the CHAT_ASSISTANT
channel and under the caller's name, sent to the AsyncAgentAssistOrchestrator.
The orchestrator runs the meeting assistant in that meeting's context, with the
tools a question from the meeting page gets, and adds the answer to the
meeting's assistant conversation, where the meeting page shows it. Here the
orchestrator is invoked synchronously, which returns the answer as well.

Nothing in the meeting assistant itself is specific to this tool.
"""

import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict

from tools.access import Caller, invoke_function, require_meeting

MAX_QUESTION_CHARS = 4000
# The orchestrator's own timeout is 60 seconds.
ANSWER_TIMEOUT_SECONDS = 75
# How long the assistant's question is kept, as strands_chat_interface keeps it.
EXPIRES_AFTER_SECONDS = 90 * 24 * 60 * 60


def execute(caller: Caller, meeting_id: str, question: Any) -> Dict[str, Any]:
    """The meeting assistant's answer to `question`, in the context of the meeting."""
    if not isinstance(question, str) or not question.strip():
        raise ValueError("question is required")
    question = question.strip()
    if len(question) > MAX_QUESTION_CHARS:
        raise ValueError(f"question is longer than {MAX_QUESTION_CHARS} characters; shorten it")
    meeting = require_meeting(meeting_id, caller)
    call_id = meeting.get("CallId") or meeting_id.strip()

    message_id = f"mcp-{uuid.uuid4()}"
    now = time.time()
    payload = {
        "CallId": call_id,
        "Channel": "CHAT_ASSISTANT",
        "SegmentId": message_id,
        "StartTime": now,
        "EndTime": now + 1,
        "Transcript": question,
        "ConversationHistory": [],
        "Speaker": caller.user_id,
        "IsPartial": False,
        "Status": "TRANSCRIBING",
        "CreatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "ExpiresAfter": int(now) + EXPIRES_AFTER_SECONDS,
        "Owner": caller.user_id,
    }
    result = invoke_function(
        os.environ.get("ASYNC_AGENT_ASSIST_ORCHESTRATOR_ARN", ""), payload, ANSWER_TIMEOUT_SECONDS
    )
    answer = result.get("message") if isinstance(result, dict) else None
    if not answer:
        raise ValueError(
            "The meeting assistant gave no answer. It may be turned off on this deployment, "
            "or it could not answer in time."
        )
    return {"meetingId": call_id, "question": question, "answer": answer}
