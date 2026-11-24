# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
"""Meetings: continue an existing one, and find the meetings that carry given
labels, and delete them.

Available as ``client.meetings``::

    session = client.meetings.reopen("<meeting ID or link to its page>")
    found = client.meetings.find_by_labels({"engagement": "acme"}, as_user="jdoe@example.com")
    result = client.meetings.delete_by_labels(
        {"engagement": "acme"}, as_user="jdoe@example.com", confirm=True
    )

``as_user`` is the LMA user the selection is made for, looked up in the stack's
Cognito user pool: an Admin's selection is every meeting, anyone else's only the
meetings they own. Deleting goes through the same per-meeting delete path as the
meetings list and the meeting page (the meeting controls function's
``deleteMeetings``, which checks the same ownership again), so every part of each
meeting is removed: its record, transcript, summary, key frames, recordings, chat
attachments, consent record, notifications, archives and Virtual Participant
records. A meeting still in progress is never deleted; it is listed as skipped.

Every call uses the caller's AWS credentials: ``cloudformation:DescribeStackResource``
on the stack and its AI stack, ``cognito-idp:AdminGetUser``,
``cognito-idp:ListUsers`` and ``cognito-idp:AdminListGroupsForUser`` on the user
pool, ``dynamodb:Query`` and ``dynamodb:BatchGetItem`` on the meetings table, and,
to delete, ``lambda:InvokeFunction`` on the meeting controls function.

``reopen`` allows one more capture session on an existing meeting, as the
streaming clients' "Continue an existing meeting…" does before it streams
(the ``reopenMeeting`` mutation; docs/websocket-streaming-api.md#continuing-an-existing-meeting).
It is signed with IAM through the stack's AppSync API, so it needs an assumed
role (an SSO or role profile).
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Mapping, Optional
from urllib.parse import unquote

from boto3.dynamodb.types import TypeDeserializer

from lma_sdk.exceptions import LMAAppSyncError, LMAConfigurationError, LMAValidationError
from lma_sdk.models.meeting import MeetingSession
from lma_sdk.models.meetings import MeetingDeleteResult, MeetingFailure, MeetingRef

if TYPE_CHECKING:
    from lma_sdk.client import LMAClient

#: The Cognito group whose members read, and may delete, every meeting.
ADMIN_GROUP = "Admin"
#: The AI stack's logical id in the main stack, and the meetings table's in it.
AI_STACK_LOGICAL_ID = "AISTACK"
MEETINGS_TABLE_LOGICAL_ID = "EventSourcingTable"
#: The index every meeting's list row is on (ItemType "call", sorted by creation).
LIST_INDEX = "TypeDateIndex"
LIST_ITEM_TYPE = "call"
#: The meeting controls function, named after the main stack.
MEETING_CONTROLS_FUNCTION = "{stack}-MeetingControlsLambda"
#: Its answer when every meeting it was given was deleted.
DELETED = "Meetings deleted successfully"
#: BatchGetItem's limit on keys per request.
BATCH_GET_MAX = 100
#: How many times keys BatchGetItem leaves unprocessed are retried, with backoff.
UNPROCESSED_RETRIES = 8

_DESERIALIZER = TypeDeserializer()


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


def parse_labels(values: Iterable[str]) -> Dict[str, str]:
    """``key=value`` arguments as a mapping; every one has to match.

    Raises:
        LMAValidationError: an argument has no ``=``, an empty key, or a key given twice.
    """
    labels: Dict[str, str] = {}
    for raw in values:
        key, sep, value = str(raw).partition("=")
        key = key.strip()
        if not sep or not key:
            raise LMAValidationError(f"{raw!r} is not a label: give it as key=value")
        if key in labels:
            raise LMAValidationError(f"the label {key!r} is given twice")
        labels[key] = value.strip()
    if not labels:
        raise LMAValidationError("give at least one label as key=value")
    return labels


def labels_of(row: Mapping[str, Any]) -> Dict[str, str]:
    """A meeting record's stored labels ([{Key, Value}]) as a mapping."""
    out: Dict[str, str] = {}
    for label in row.get("Labels") or []:
        if isinstance(label, Mapping) and isinstance(label.get("Key"), str):
            out[label["Key"]] = str(label.get("Value") or "")
    return out


def _plain(item: Mapping[str, Any]) -> Dict[str, Any]:
    return {key: _DESERIALIZER.deserialize(value) for key, value in item.items()}


class MeetingOperations:
    """Continue a meeting, and find and delete meetings by their labels, as ``client.meetings``."""

    def __init__(self, client: LMAClient) -> None:
        self._client = client
        self._table_name: Optional[str] = None

    # ── continuing a meeting ───────────────────────────────────────────────

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

    # ── who the selection is for ───────────────────────────────────────────

    def user_scope(self, as_user: str) -> tuple[str, bool]:
        """The Cognito user name ``as_user`` names, and whether they are in the Admin group."""
        user = self._client.vp.resolve_user(as_user)
        cognito = self._client.session.client("cognito-idp")
        pool_id = self._client.vp.user_pool_id
        groups: List[str] = []
        token: Optional[str] = None
        while True:
            kwargs: Dict[str, Any] = {"UserPoolId": pool_id, "Username": user.username}
            if token:
                kwargs["NextToken"] = token
            response = cognito.admin_list_groups_for_user(**kwargs)
            groups += [group.get("GroupName", "") for group in response.get("Groups") or []]
            token = response.get("NextToken")
            if not token:
                break
        return user.username, ADMIN_GROUP in groups

    def account_id(self) -> str:
        """The AWS account the caller's credentials are in, or "" when it cannot be read."""
        try:
            return str(self._client.session.client("sts").get_caller_identity().get("Account") or "")
        except Exception:  # noqa: BLE001
            return ""

    # ── finding the meetings ───────────────────────────────────────────────

    @property
    def table_name(self) -> str:
        """The meetings table, found through the stack's resources."""
        if self._table_name:
            return self._table_name
        cfn = self._client.session.client("cloudformation")
        stack = self._client._require_stack()  # noqa: SLF001 -- the client's own helper
        try:
            ai_stack = cfn.describe_stack_resource(
                StackName=stack, LogicalResourceId=AI_STACK_LOGICAL_ID
            )["StackResourceDetail"]["PhysicalResourceId"]
            table = cfn.describe_stack_resource(
                StackName=ai_stack, LogicalResourceId=MEETINGS_TABLE_LOGICAL_ID
            )["StackResourceDetail"]["PhysicalResourceId"]
        except Exception as err:  # noqa: BLE001
            raise LMAConfigurationError(
                f"Could not find the meetings table of stack {stack}: {err}"
            ) from err
        self._table_name = table
        return table

    def _list_rows(self) -> List[Dict[str, Any]]:
        """Every meeting's list row, oldest first: the whole index, with no page limit."""
        dynamodb = self._client.session.client("dynamodb")
        rows: List[Dict[str, Any]] = []
        kwargs: Dict[str, Any] = {
            "TableName": self.table_name,
            "IndexName": LIST_INDEX,
            "KeyConditionExpression": "ItemType = :type",
            "ExpressionAttributeValues": {":type": {"S": LIST_ITEM_TYPE}},
        }
        while True:
            response = dynamodb.query(**kwargs)
            rows += [_plain(item) for item in response.get("Items") or []]
            last = response.get("LastEvaluatedKey")
            if not last:
                return rows
            kwargs["ExclusiveStartKey"] = last

    def _records(self, call_ids: List[str]) -> Dict[str, Dict[str, Any]]:
        """The meeting records of ``call_ids``: their labels, status and owner."""
        dynamodb = self._client.session.client("dynamodb")
        found: Dict[str, Dict[str, Any]] = {}
        for start in range(0, len(call_ids), BATCH_GET_MAX):
            keys = [
                {"PK": {"S": f"c#{call_id}"}, "SK": {"S": f"c#{call_id}"}}
                for call_id in call_ids[start : start + BATCH_GET_MAX]
            ]
            request: Dict[str, Any] = {
                self.table_name: {
                    "Keys": keys,
                    "ProjectionExpression": "CallId, #owner, #status, Labels",
                    "ExpressionAttributeNames": {"#owner": "Owner", "#status": "Status"},
                }
            }
            attempt = 0
            while request:
                response = dynamodb.batch_get_item(RequestItems=request)
                for item in response.get("Responses", {}).get(self.table_name, []):
                    record = _plain(item)
                    found[str(record.get("CallId"))] = record
                request = response.get("UnprocessedKeys") or {}
                if request:
                    # Keys DynamoDB did not process (throttling): back off, then retry them.
                    attempt += 1
                    if attempt > UNPROCESSED_RETRIES:
                        raise LMAConfigurationError(
                            "The meetings table kept refusing reads; try again later"
                        )
                    time.sleep(min(0.1 * 2**attempt, 5.0))
        return found

    def find_by_labels(
        self, labels: Mapping[str, str], as_user: str, *, admin: Optional[bool] = None
    ) -> List[MeetingRef]:
        """The meetings that carry every one of ``labels`` and that ``as_user`` may delete.

        ``admin`` is looked up when not given: an Admin's selection is every
        meeting, anyone else's only the meetings they own. When ``admin`` is
        given, ``as_user`` is not looked up and must already be the Cognito user
        name (as ``user_scope`` returns it), since that is what a meeting's owner is.
        """
        if not labels:
            raise LMAValidationError("give at least one label")
        if admin is None:
            as_user, admin = self.user_scope(as_user)
        rows = [
            row
            for row in self._list_rows()
            if row.get("CallId") and (admin or row.get("Owner") == as_user)
        ]
        records = self._records([str(row["CallId"]) for row in rows])
        found: List[MeetingRef] = []
        for row in rows:
            record = records.get(str(row["CallId"]))
            if record is None:
                continue
            carried = labels_of(record)
            if any(carried.get(key) != value for key, value in labels.items()):
                continue
            # Decided on the record too, which is what the delete path checks.
            if not admin and record.get("Owner") != as_user:
                continue
            found.append(
                MeetingRef(
                    call_id=str(row["CallId"]),
                    owner=str(record.get("Owner") or row.get("Owner") or ""),
                    created_at=str(row.get("CreatedAt") or ""),
                    status=str(record.get("Status") or ""),
                    labels=carried,
                    list_pk=str(row.get("PK") or ""),
                    list_sk=str(row.get("SK") or ""),
                )
            )
        return found

    # ── deleting them ──────────────────────────────────────────────────────

    def _delete_one(self, meeting: MeetingRef, username: str, admin: bool) -> Optional[str]:
        """Delete one meeting through the meeting controls function; the error, or None."""
        event = {
            "identity": {"username": username, "groups": [ADMIN_GROUP] if admin else []},
            "arguments": {
                "input": {
                    "Calls": [
                        {
                            "CallId": meeting.call_id,
                            "ListPK": meeting.list_pk,
                            "ListSK": meeting.list_sk,
                        }
                    ]
                }
            },
            "info": {"fieldName": "deleteMeetings"},
        }
        function = MEETING_CONTROLS_FUNCTION.format(
            stack=self._client._require_stack()  # noqa: SLF001
        )
        try:
            response = self._client.session.client("lambda").invoke(
                FunctionName=function,
                InvocationType="RequestResponse",
                Payload=json.dumps(event).encode("utf-8"),
            )
        except Exception as err:  # noqa: BLE001
            return f"could not invoke {function}: {err}"
        body = response["Payload"].read() if response.get("Payload") is not None else b""
        try:
            answer = json.loads(body or b"null")
        except ValueError:
            answer = None
        if response.get("FunctionError"):
            message = answer.get("errorMessage") if isinstance(answer, dict) else None
            return message or "the delete failed"
        result = answer.get("Result") if isinstance(answer, dict) else None
        return None if result == DELETED else (result or "the delete gave no result")

    def delete_by_labels(
        self, labels: Mapping[str, str], as_user: str, *, confirm: bool = False
    ) -> MeetingDeleteResult:
        """Delete every meeting ``find_by_labels`` finds, unless it is still in progress.

        Without ``confirm`` this is a dry run: it finds the meetings and changes
        nothing. Each meeting is deleted on its own, so one failure does not stop
        the rest; the result lists what was deleted and what failed.
        """
        username, admin = self.user_scope(as_user)
        found = self.find_by_labels(labels, username, admin=admin)
        result = MeetingDeleteResult(
            dry_run=not confirm,
            stack=self._client._require_stack(),  # noqa: SLF001
            region=self._client.region,
            account=self.account_id(),
            user=username,
            scope="all" if admin else "own",
            labels=dict(labels),
            matched=[meeting for meeting in found if not meeting.in_progress],
            skipped_in_progress=[meeting for meeting in found if meeting.in_progress],
        )
        if not confirm:
            return result
        for meeting in result.matched:
            error = self._delete_one(meeting, username, admin)
            if error is None:
                result.deleted.append(meeting.call_id)
            else:
                result.failed.append(MeetingFailure(call_id=meeting.call_id, error=error))
        return result


def _refusal(err: LMAAppSyncError) -> LMAAppSyncError:
    """The AppSync error with the server's own message, for a refusal the
    resolver named (``error_type``); the error as it is otherwise."""
    if not err.error_type:
        return err
    message = err.details["errors"][0].get("message") or err.message
    return LMAAppSyncError(message, details=err.details)
