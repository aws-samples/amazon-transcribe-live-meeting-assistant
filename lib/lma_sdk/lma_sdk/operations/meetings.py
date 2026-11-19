# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
"""Meeting operations (IAM-authenticated).

``reopen`` allows one more capture session on an existing meeting, as the
streaming clients' "Continue an existing meeting…" does before it streams
(the ``reopenMeeting`` mutation; docs/websocket-streaming-api.md#continuing-an-existing-meeting).
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import unquote

from lma_sdk.exceptions import LMAAppSyncError, LMAValidationError
from lma_sdk.models.meeting import MeetingSession

if TYPE_CHECKING:
    from lma_sdk.client import LMAClient

_REOPEN_MUTATION = """
mutation ReopenMeeting($input: ReopenMeetingInput!) {
  reopenMeeting(input: $input) {
    CallId
    SessionNumber
    SessionOffsetSeconds
    ReopenedAt
  }
}
""".strip()

_MEETING_PAGE_MARKER = "#/calls/"


def meeting_id_from(value: str) -> str | None:
    """The meeting ID in a meeting ID or a link to the meeting's page in LMA.

    ``https://…/#/calls/<URL-encoded meeting ID>`` gives the decoded ID (a
    query or a trailing slash copied with the link is left out); any other
    link gives None; anything else is taken as the ID itself, trimmed. The
    desktop apps, Stream Audio and the browser extension read what is pasted
    the same way.
    """
    text = (value or "").strip()
    if not text:
        return None
    at = text.find(_MEETING_PAGE_MARKER)
    if at >= 0:
        encoded = text[at + len(_MEETING_PAGE_MARKER) :].split("?", 1)[0].rstrip("/")
        decoded = unquote(encoded).strip()
        return decoded or None
    if text.lower().startswith(("http://", "https://")):
        return None
    return text


class MeetingOperations:
    """Operations on LMA meetings."""

    def __init__(self, client: LMAClient) -> None:
        self._client = client

    def reopen(self, call_id: str) -> MeetingSession:
        """Allow the next capture session on an existing meeting.

        ``call_id`` is the meeting ID, or a link to the meeting's page. The
        answer names the session a streaming client sends as ``sessionNumber``
        in its START frame, with the meeting's CallId, to continue the meeting,
        and where that session starts on the transcript timeline. The meeting
        stays as it is until that session's START arrives.

        The server refuses a meeting that does not exist, one still being
        captured, and one whose transcript has been deleted. A refusal is
        raised as ``LMAAppSyncError``; its ``error_type`` is the server's
        (``MeetingNotFound``, ``Unauthorized``, ``TranscriptDeleted``,
        ``MeetingInProgress``, ``MeetingChanged`` or ``BadRequest``) and its
        message the server's. Signed with IAM, the call needs an assumed role
        (an SSO or role profile), which may reopen any meeting.
        """
        meeting_id = meeting_id_from(call_id)
        if not meeting_id:
            raise LMAValidationError(
                "Give the meeting ID, or the link to the meeting's page in LMA."
            )
        try:
            data = self._client.appsync.graphql(
                query=_REOPEN_MUTATION, variables={"input": {"CallId": meeting_id}}
            )
        except LMAAppSyncError as err:
            refusal = _refusal(err)
            if refusal is err:
                raise
            raise refusal from err
        answer = (data or {}).get("reopenMeeting")
        if not answer:
            raise LMAAppSyncError(
                f"reopenMeeting returned no session for {meeting_id!r}",
                details={"response": data},
            )
        return MeetingSession(
            call_id=answer["CallId"],
            session_number=int(answer["SessionNumber"]),
            session_offset_seconds=float(answer["SessionOffsetSeconds"]),
            reopened_at=answer.get("ReopenedAt"),
        )


def _refusal(err: LMAAppSyncError) -> LMAAppSyncError:
    """The AppSync error with the server's own message, for a refusal the
    resolver named (``error_type``); the error as it is otherwise."""
    if not err.error_type:
        return err
    message = err.details["errors"][0].get("message") or err.message
    return LMAAppSyncError(message, details=err.details)
