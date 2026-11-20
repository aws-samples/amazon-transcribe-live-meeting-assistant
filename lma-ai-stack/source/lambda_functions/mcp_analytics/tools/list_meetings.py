# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""
Tool 4: list_meetings
List meetings from DynamoDB with filters

The meetings are found by walking the TypeDateIndex, which is ordered by when
each meeting was created; ``startDate`` and ``endDate`` bound that walk. Every
other filter -- access, status, participant, labels, audience, ``updatedSince``
and ``endedSince`` -- is decided on the meeting's own row as it is read, before
the limit. A meeting created before the walk's start is not checked, so when
``updatedSince`` or ``endedSince`` is given without a ``startDate`` the walk
starts a day before that time if that is earlier than the default of seven days
ago (``CREATED_LOOK_BACK``), and the answer says where it started.
"""

import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, FrozenSet, List, Mapping, NamedTuple, Optional, Tuple

import boto3

from tools.access import Caller, can_read
from tools.url_helper import get_meeting_url, get_virtual_participant_url

logger = logging.getLogger()

# The status filter's accepted values. IN_PROGRESS is the pair of statuses a
# meeting has while it is running; every other value is a status as stored.
IN_PROGRESS_STATUSES = frozenset({"STARTED", "TRANSCRIBING"})
STATUS_FILTERS: Dict[str, Optional[FrozenSet[str]]] = {
    "ALL": None,
    "IN_PROGRESS": IN_PROGRESS_STATUSES,
    "STARTED": frozenset({"STARTED"}),
    "TRANSCRIBING": frozenset({"TRANSCRIBING"}),
    "ERRORED": frozenset({"ERRORED"}),
    "ENDED": frozenset({"ENDED"}),
}

# The index is walked at most this many pages, whatever the filters leave.
MAX_PAGES = 10
DEFAULT_LIMIT = 20
# The most meetings a call can return: a walk reads at most MAX_PAGES pages of
# 100 index rows, so a larger ``limit`` could never be filled.
MAX_LIMIT = 1000
# A walk that filters rows reads index pages this large; one that does not
# keeps asking for as many rows as the caller wants.
FILTERED_PAGE_SIZE = 100

# Whole meeting rows are read in batches. A walk without a filter reads batches
# as large as the rows still needed, but never below BATCH_GET_MIN and never
# above BATCH_GET_MAX (the most keys one BatchGetItem accepts). A filtered walk
# decides on a whole index page per request, asking for only a few attributes
# (this keeps each response small; DynamoDB still reads, and bills, whole
# items), then reads the whole rows of the matches, no more than still needed.
# A call that filters therefore reads at least one index page (100 rows) and, if
# few rows match, up to MAX_PAGES pages (1,000 rows); a non-admin caller's call
# always filters.
BATCH_GET_MIN = 25
BATCH_GET_MAX = 100
# Keys DynamoDB hands back unprocessed are asked for again this many times in
# all, pausing 0.05 * 2**attempt seconds between the requests.
BATCH_GET_ATTEMPTS = 4

UNREADABLE_NOTE = "Some meetings could not be read; try again."
# How many index rows a walk checks depends on its page size, so the note names
# no number.
TRUNCATED_NOTE = (
    "Only the most recent meetings in the date range were checked, so older "
    "meetings that match may not be listed."
)
SEARCH_FAILED_NOTE = "The search could not be completed; try again."

# A filtered walk first asks for only the attributes its filters look at (and the
# key, to match each row to its meeting), which keeps the response small; full
# rows are asked for the matches only. The access rule reads Owner and
# SharedWith. Owner and Status are reserved words, hence the names.
DECISION_PROJECTION: Dict[str, Any] = {
    "ProjectionExpression": "#pk, #id, #owner, #shared, #status, #participants",
    "ExpressionAttributeNames": {
        "#pk": "PK",
        "#id": "CallId",
        "#owner": "Owner",
        "#shared": "SharedWith",
        "#status": "Status",
        "#participants": "Participants",
    },
}

# The further attributes a filter reads, added to the projection only when that
# filter is given, so a call without it reads no more than before.
FILTER_ATTRIBUTES = {
    "labels": ("#labels", "Labels"),
    "audience": ("#audience", "Audience"),
    "updated": ("#updated", "UpdatedAt"),
}

AUDIENCES = ("internal", "external")
# How long before an updatedSince or endedSince time the walk of the creation
# index starts, when no startDate is given: a meeting that ended or changed
# after that time was usually created at most this long before it.
CREATED_LOOK_BACK = timedelta(days=1)
DEFAULT_CREATED_RANGE = timedelta(days=7)
SINCE_NOTE = (
    "Only meetings created since {created_from} were checked; pass startDate to look "
    "further back for meetings changed or ended since the time given."
)
VP_LOOKUP_NOTE = (
    "The Virtual Participant table is larger than one read covers, so virtualParticipantId "
    "may be missing from some meetings."
)


def decision_projection(attributes: Tuple[str, ...] = ()) -> Dict[str, Any]:
    """DECISION_PROJECTION with the attributes of the given filters added."""
    if not attributes:
        return DECISION_PROJECTION
    names = dict(DECISION_PROJECTION["ExpressionAttributeNames"])
    expression = DECISION_PROJECTION["ProjectionExpression"]
    for key in attributes:
        placeholder, attribute = FILTER_ATTRIBUTES[key]
        names[placeholder] = attribute
        expression += f", {placeholder}"
    return {"ProjectionExpression": expression, "ExpressionAttributeNames": names}


def labels_filter(value: Any) -> Optional[Dict[str, str]]:
    """The ``labels`` argument: an object of label keys to values, all of which must match."""
    if value is None or value == {}:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("labels must be an object of label keys to string values")
    try:
        # pylint: disable-next=import-error,import-outside-toplevel
        from lma_meeting_preparation.rules import LABEL_KEY
    except ImportError:  # the layer is absent only outside Lambda
        LABEL_KEY = None  # pylint: disable=invalid-name
    wanted: Dict[str, str] = {}
    for key, label in value.items():
        if not isinstance(key, str) or (LABEL_KEY is not None and not LABEL_KEY.fullmatch(key)):
            raise ValueError(f"{key!r} is not a label key")
        if not isinstance(label, str):
            raise ValueError(f"The value of label {key} must be a string")
        wanted[key] = label
    return wanted


def audience_filter(value: Any) -> Optional[str]:
    """The ``audience`` argument: internal or external; blank is no filter."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, str) and value.strip().lower() in AUDIENCES:
        return value.strip().lower()
    raise ValueError("audience must be internal or external")


def since_filter(value: Any, name: str) -> Optional[datetime]:
    """An ISO 8601 time argument, in UTC; blank is no filter."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name} must be an ISO 8601 date and time")
    try:
        moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(
            f"{name} must be an ISO 8601 date and time, for example 2026-11-20T09:00:00Z"
        ) from error
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def stored_time(value: Any) -> Optional[datetime]:
    """A stored ISO 8601 time, in UTC; None when absent or unreadable."""
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def labels_of(meeting: Mapping[str, Any]) -> Dict[str, str]:
    """The meeting's stored labels ([{Key, Value}]) as an object."""
    out: Dict[str, str] = {}
    for label in meeting.get("Labels") or []:
        if isinstance(label, Mapping) and isinstance(label.get("Key"), str):
            out[label["Key"]] = str(label.get("Value") or "")
    return out


def created_from(start_date: Optional[str], *since: Optional[datetime]) -> Optional[str]:
    """Where the walk of the creation index starts when no startDate is given.

    The default, seven days ago, or a day before the earliest since-time given
    when that is earlier; None when there is no since-time (the default applies).
    """
    given = [moment for moment in since if moment is not None]
    if start_date or not given:
        return None
    default = datetime.now(timezone.utc) - DEFAULT_CREATED_RANGE
    start = min([default, *(moment - CREATED_LOOK_BACK for moment in given)])
    return start.strftime("%Y-%m-%dT%H:%M:%S")


def limit_from(limit: Any) -> int:
    """The number of meetings a ``limit`` argument asks for, from 1 to MAX_LIMIT.

    The tool schema says ``number``, so a client may send ``20.0`` or ``"20"``.
    None (or no value) means the default. A boolean, a fraction, text that is
    not a whole number, or any other type is refused.
    """
    if limit is None:
        return DEFAULT_LIMIT
    value = None
    if isinstance(limit, int) and not isinstance(limit, bool):
        value = limit
    elif isinstance(limit, float):
        value = int(limit) if limit.is_integer() else None
    elif isinstance(limit, str):
        try:
            number = float(limit.strip())
        except ValueError:
            number = None
        if number is not None and number.is_integer():
            value = int(number)
    if value is None:
        raise ValueError("limit must be an integer")
    return max(1, min(value, MAX_LIMIT))


class WalkResult(NamedTuple):
    """What ``walk_date_range`` found."""

    # The meeting rows, newest first.
    rows: List[Dict]
    # How many rows could not be read.
    unreadable: int
    # Whether the page cap ended the walk with index rows still left.
    truncated: bool
    # Whether an index page could not be read (the walk then ended early).
    failed: bool


def execute(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    participant: Optional[str] = None,
    status: str = "ALL",
    limit: int = 20,
    user_id: str = None,
    is_admin: bool = False,
    labels: Any = None,
    audience: Any = None,
    updated_since: Any = None,
    ended_since: Any = None,
) -> Dict[str, Any]:
    """
    List meetings from DynamoDB with optional filters.
    Enforces user-based access control.

    Args:
        start_date: Optional ISO 8601 start date
        end_date: Optional ISO 8601 end date
        participant: Optional participant name filter
        status: Meeting status filter: 'ALL' (or None), 'IN_PROGRESS' (started or
            transcribing), 'STARTED', 'TRANSCRIBING', 'ERRORED' or 'ENDED'; not
            case sensitive
        limit: Maximum number of meetings to return: a whole number from 1 to
            1,000 (more is read as 1,000); None means 20
        user_id: User ID for access control
        is_admin: Whether user is admin
        labels: Optional object of label keys to values; every pair must match
        audience: Optional internal or external
        updated_since: Optional ISO 8601 time; meetings changed at or after it
        ended_since: Optional ISO 8601 time; meetings that ended at or after it

    Returns:
        Dict with list of meetings and metadata
    """
    statuses = statuses_for(status)
    limit = limit_from(limit)
    wanted_labels = labels_filter(labels)
    wanted_audience = audience_filter(audience)
    updated_after = since_filter(updated_since, "updatedSince")
    ended_after = since_filter(ended_since, "endedSince")
    walk_from = created_from(start_date, updated_after, ended_after)
    extra = MeetingFilters(wanted_labels, wanted_audience, updated_after, ended_after)

    dynamodb = boto3.resource("dynamodb")
    table_name = os.environ.get("CALLS_TABLE")
    table = dynamodb.Table(table_name)

    try:
        # Owner, participant and status are decided while the rows are read, so
        # that only meetings the caller can see count toward the limit.
        include = build_filter(user_id, is_admin, participant, statuses, extra)

        # Query strategy depends on filters
        if start_date and end_date:
            # Use date-based query for efficiency
            start, end = start_date, end_date
        elif walk_from:
            start, end = walk_from, end_date or datetime.utcnow().strftime("%Y-%m-%d")
        else:
            # Query recent meetings using today's date shards
            today = datetime.utcnow()
            # Query last 7 days to ensure we find meetings
            start = (today - timedelta(days=7)).strftime("%Y-%m-%d")
            end = today.strftime("%Y-%m-%d")
        found = walk_date_range(
            table, start, end, limit, include, decision_projection(extra.attributes())
        )
        meetings, unreadable, truncated, failed = (
            found.rows,
            found.unreadable,
            found.truncated,
            found.failed,
        )

        vp_ids, vp_lookup_complete = virtual_participant_ids(meetings)

        # Format response
        result = {
            "meetings": [format_meeting(m, vp_ids.get(m.get("CallId"))) for m in meetings],
            "count": len(meetings),
            "filters": {
                "startDate": start_date,
                "endDate": end_date,
                "participant": participant,
                "status": status,
                "labels": wanted_labels,
                "audience": wanted_audience,
                "updatedSince": updated_since or None,
                "endedSince": ended_since or None,
            },
        }
        notes = []
        if walk_from:
            result["filters"]["createdFrom"] = walk_from
            notes.append(SINCE_NOTE.format(created_from=walk_from))
        if not vp_lookup_complete:
            notes.append(VP_LOOKUP_NOTE)
        if truncated:
            result["truncated"] = True
            notes.append(TRUNCATED_NOTE)
        if unreadable:
            notes.append(UNREADABLE_NOTE)
        if failed:
            notes.append(SEARCH_FAILED_NOTE)
        if notes:
            result["note"] = " ".join(notes)

        logger.info(f"Listed {len(meetings)} meetings for user {user_id}")
        return result

    except Exception as e:
        logger.error(f"Error listing meetings: {e}")
        raise ValueError(f"Failed to list meetings: {str(e)}")


class MeetingFilters(NamedTuple):
    """The filters on a meeting's preparation and times, each None when not given."""

    labels: Optional[Dict[str, str]] = None
    audience: Optional[str] = None
    updated_since: Optional[datetime] = None
    ended_since: Optional[datetime] = None

    def given(self) -> bool:
        return any(value is not None for value in self)

    def attributes(self) -> Tuple[str, ...]:
        """The FILTER_ATTRIBUTES these filters read."""
        names = []
        if self.labels:
            names.append("labels")
        if self.audience:
            names.append("audience")
        if self.updated_since or self.ended_since:
            names.append("updated")
        return tuple(names)

    def match(self, meeting: Mapping[str, Any]) -> bool:
        if self.labels:
            stored = labels_of(meeting)
            if any(stored.get(key) != value for key, value in self.labels.items()):
                return False
        if self.audience and meeting.get("Audience") != self.audience:
            return False
        updated = stored_time(meeting.get("UpdatedAt"))
        if self.updated_since and (updated is None or updated < self.updated_since):
            return False
        if self.ended_since:
            # A meeting has no end time of its own; an ended meeting's UpdatedAt
            # is its end or a later change, so no meeting that ended since the
            # time is left out.
            if meeting.get("Status") != "ENDED" or updated is None or updated < self.ended_since:
                return False
        return True


def virtual_participant_ids(meetings: List[Dict[str, Any]]) -> Tuple[Dict[str, str], bool]:
    """The newest Virtual Participant of each meeting, by CallId, and whether all were read.

    One bounded scan of the Virtual Participant table (virtual_participants),
    made only when there are meetings to name and the table is configured.
    """
    if not meetings or not os.environ.get("VP_TABLE_NAME"):
        return {}, True
    wanted = {m.get("CallId") for m in meetings if m.get("CallId")}
    try:
        # pylint: disable-next=import-outside-toplevel
        from tools.virtual_participants import vps_by_call

        return vps_by_call(wanted)
    except Exception as e:  # the list is still answered without the ids
        logger.warning("Could not read the Virtual Participant table (%s)", failure_class(e))
        return {}, False


def statuses_for(status: Optional[str]) -> Optional[FrozenSet[str]]:
    """The stored statuses a ``status`` argument stands for; None means every status.

    Not case sensitive, surrounding spaces are ignored, and a blank value is the
    same as ALL (clients often send an empty string for an argument they do not
    set). Anything else that is not an accepted value is refused, naming the
    accepted ones.
    """
    if status is None:
        return None
    if isinstance(status, str):
        key = status.strip().upper()
        if not key:
            return None
        if key in STATUS_FILTERS:
            return STATUS_FILTERS[key]
    raise ValueError("status must be one of: " + ", ".join(STATUS_FILTERS))


def build_filter(
    user_id: Optional[str],
    is_admin: bool,
    participant: Optional[str],
    statuses: Optional[FrozenSet[str]],
    extra: Optional[MeetingFilters] = None,
) -> Optional[Callable[[Dict[str, Any]], bool]]:
    """The test a meeting row has to pass to be listed, or None when every row is.

    Access is the rule of the meeting page (tools/access.py can_read): an Admin
    sees every meeting; anyone else sees the meetings they own and the ones
    shared with them. A caller with no identity sees none.
    """
    extra = extra or MeetingFilters()
    if is_admin and not participant and statuses is None and not extra.given():
        return None
    caller = Caller(user_id=user_id or "", is_admin=bool(is_admin))

    def include(meeting: Dict[str, Any]) -> bool:
        if not can_read(meeting, caller):
            return False
        if participant and not participant_in_meeting(meeting, participant):
            return False
        if not extra.match(meeting):
            return False
        if statuses is None:
            return True
        # A Status that is not a string (a corrupt row) matches no status and
        # must not fail the whole call.
        status = meeting.get("Status")
        return isinstance(status, str) and status in statuses

    return include


def query_by_date_range(table, start_date: str, end_date: str, limit: int) -> List[Dict]:
    """
    Query meetings by date range using the TypeDateIndex GSI.

    The GSI is keyed by ItemType (HASH, always "call") and SK (RANGE, format
    "ts#<ISO8601>#id#<CallId>").  A single query covers the whole date range
    and paginates via LastEvaluatedKey — no shard fan-out.  ``execute`` calls
    ``walk_date_range``, which does the work and takes a row filter.
    """
    return walk_date_range(table, start_date, end_date, limit).rows


def walk_date_range(
    table,
    start_date: str,
    end_date: str,
    limit: int,
    include: Optional[Callable[[Dict[str, Any]], bool]] = None,
    projection: Optional[Dict[str, Any]] = None,
) -> WalkResult:
    """
    Walk the TypeDateIndex GSI, newest first, and collect up to ``limit`` meetings.

    Returns a ``WalkResult``: the meeting rows, how many rows could not be read,
    whether the page cap ended the walk with index rows still left, and whether
    an index page could not be read (the walk then ends early).

    The GSI is keyed by ItemType (HASH, always "call") and SK (RANGE, format
    "ts#<ISO8601>#id#<CallId>").  A single query covers the whole date range
    and paginates via LastEvaluatedKey — no shard fan-out.  An index row is
    not the meeting's own row and carries only a few of its attributes (not
    Status), so every meeting row is read before ``include`` decides on it:
    first only the attributes in DECISION_PROJECTION (a smaller response;
    DynamoDB still reads and bills whole items), one request per index page,
    then, in full and in index order, the rows that match, no more than still
    needed.  Access is decided on the meeting's own row, in both steps, and not
    on the index row's copy of Owner and SharedWith.  Without ``include`` the
    rows are read in full at once.  At most ``limit`` rows are returned.
    """
    from boto3.dynamodb.conditions import Key

    meetings: List[Dict] = []
    meeting_ids = set()
    unreadable = 0

    # Parse dates — fall back to "last 7 days" / "now" when unset.
    start_iso = (
        start_date if start_date else (datetime.utcnow() - timedelta(days=7)).isoformat() + "Z"
    )
    end_iso = end_date if end_date else datetime.utcnow().isoformat() + "Z"

    # SK format is `ts#<ISO8601>#id#<CallId>`. To get an inclusive
    # date-range slice on the GSI we use:
    #   lower bound: "ts#<start_iso>"   (any SK starting with this date sorts
    #                                    at or after this point)
    #   upper bound: "ts#<end_iso>~"    ("~" / 0x7E sorts *after* every char
    #                                    that can legally follow the date in
    #                                    the SK — including "T" (0x54), which
    #                                    is the literal that appears in real
    #                                    SKs like "ts#2026-05-27T17:34:..." )
    #
    # Earlier code used "ts#<end_iso>#~" (an extra "#"), which silently
    # excluded every meeting whose SK contained a "T" right after the date,
    # because "T" (0x54) > "#" (0x23). That caused list_meetings to return
    # zero rows for any caller that didn't pass an explicit end_date past
    # midnight. Do not re-introduce the stray "#".
    sk_lo = f"ts#{start_iso}"
    sk_hi = f"ts#{end_iso}~"

    page_size = FILTERED_PAGE_SIZE if include is not None else min(limit, 100)
    query_kwargs = {
        "IndexName": "TypeDateIndex",
        "KeyConditionExpression": (Key("ItemType").eq("call") & Key("SK").between(sk_lo, sk_hi)),
        "ScanIndexForward": False,  # newest first
        "Limit": page_size,
    }

    # Matches of a filtered walk whose whole rows are not read yet, newest first.
    pending: List[str] = []
    # Index rows a filtered walk could not decide on, as positions in the walk.
    undecided: List[int] = []
    order: Dict[str, int] = {}
    last_listed = -1

    def take_pending(final: bool) -> None:
        """Read the whole rows of pending matches, at most as many as still needed.

        Waits until enough matches are pending to fill the limit, unless the walk
        is over, so that the whole rows of a call take as few requests as they
        can. A row that changed or went away since it was decided is decided again.
        """
        nonlocal unreadable, last_listed
        while pending and len(meetings) < limit:
            needed = limit - len(meetings)
            if not final and len(pending) < needed:
                return
            wanted = pending[: min(BATCH_GET_MAX, needed)]
            del pending[: len(wanted)]
            rows, failed = read_meetings(table, wanted)
            unreadable += failed
            for call_id in wanted:
                meeting = rows.get(call_id)
                if meeting is not None and include(meeting):
                    meetings.append(meeting)
                    last_listed = order[call_id]

    # Bound pagination so a very sparse / restrictive caller filter can't
    # force an unbounded scan of the GSI.  Matches the resolver's MAX_PAGES.
    pages = 0
    last_key = None
    page_failed = False
    while len(meetings) < limit and pages < MAX_PAGES:
        if last_key:
            query_kwargs["ExclusiveStartKey"] = last_key
        try:
            response = table.query(**query_kwargs)
        except Exception as e:
            logger.warning("GSI query failed (%s)", failure_class(e))
            page_failed = True
            break

        page_ids = []
        for item in response.get("Items", []):
            call_id = item.get("CallId")
            if not call_id or call_id in meeting_ids:
                continue
            meeting_ids.add(call_id)
            order[call_id] = len(order)
            page_ids.append(call_id)

        if include is None:
            position = 0
            while position < len(page_ids) and len(meetings) < limit:
                size = min(BATCH_GET_MAX, max(BATCH_GET_MIN, limit - len(meetings)))
                batch = page_ids[position : position + size]
                position += len(batch)
                rows, failed = read_meeting_rows(table, batch)
                # The batch can hold rows beyond the answer; a failed read counts
                # only when the row would have been listed.
                for call_id in batch:
                    meeting = rows.get(call_id)
                    if meeting is None:
                        unreadable += call_id in failed
                        continue
                    meetings.append(meeting)
                    if len(meetings) >= limit:
                        break
        else:
            # The response holds only the few attributes the filters read, so
            # one request decides a whole page (up to 100 rows).
            for position in range(0, len(page_ids), BATCH_GET_MAX):
                batch = page_ids[position : position + BATCH_GET_MAX]
                decided, failed = read_meeting_rows(table, batch, projection or DECISION_PROJECTION)
                undecided.extend(order[call_id] for call_id in failed)
                pending.extend(
                    call_id for call_id in batch if call_id in decided and include(decided[call_id])
                )
            take_pending(final=False)

        last_key = response.get("LastEvaluatedKey")
        pages += 1
        if not last_key:
            break

    if include is not None:
        take_pending(final=True)
        # A row that could not be read counts when it could have been listed:
        # always while the list is short, otherwise only when it is newer than
        # the last meeting listed.
        full = len(meetings) >= limit
        unreadable += sum(1 for at in undecided if not full or at < last_listed)

    truncated = False
    if last_key and pages >= MAX_PAGES and len(meetings) < limit:
        logger.info(
            "walk_date_range: hit MAX_PAGES=%d cap (collected=%d)", MAX_PAGES, len(meetings)
        )
        truncated = rows_left(table, query_kwargs, last_key)
    return WalkResult(meetings[:limit], unreadable, truncated, page_failed)


def rows_left(table, query_kwargs: Dict[str, Any], last_key: Dict[str, Any]) -> bool:
    """Whether the index has a row after ``last_key``.

    DynamoDB returns a key after a page that ends exactly at the end of the
    range, so the key alone does not say. When the check itself fails the answer
    is yes: the list may be incomplete.
    """
    try:
        probe = dict(query_kwargs, Limit=1, ExclusiveStartKey=last_key)
        return bool(table.query(**probe).get("Items"))
    except Exception as e:
        logger.warning("Could not check for more index rows (%s)", failure_class(e))
        return True


def failure_class(error: Exception) -> str:
    """The class of a failure, with the AWS error code when there is one.

    The message of an AWS error can name the table or the account, so it is not
    logged.
    """
    code = getattr(error, "response", None)
    code = code.get("Error", {}).get("Code") if isinstance(code, dict) else None
    return f"{type(error).__name__}: {code}" if code else type(error).__name__


def get_meetings_by_ids(
    table, call_ids: List[str], projection: Optional[Dict[str, Any]] = None
) -> Dict[str, Dict]:
    """Read the meeting rows of up to 100 CallIds with BatchGetItem.

    Returns the rows DynamoDB returned, by CallId. Keys it left unprocessed are
    asked for again, up to BATCH_GET_ATTEMPTS requests in all. A request that
    fails is logged, not raised: the caller gets what was read so far.
    ``projection`` (``ProjectionExpression`` and ``ExpressionAttributeNames``)
    limits the attributes read; it must include ``PK``.
    """
    if len(call_ids) > BATCH_GET_MAX:
        raise ValueError(f"at most {BATCH_GET_MAX} meetings can be read at once")
    found: Dict[str, Dict] = {}
    if not call_ids:
        return found
    keys = [{"PK": f"c#{call_id}", "SK": f"c#{call_id}"} for call_id in call_ids]
    for attempt in range(BATCH_GET_ATTEMPTS):
        try:
            request = {"Keys": keys, **(projection or {})}
            response = table.meta.client.batch_get_item(RequestItems={table.name: request})
        except Exception as e:
            logger.warning("Batch read of %d meetings failed (%s)", len(keys), failure_class(e))
            break
        for item in response.get("Responses", {}).get(table.name, []):
            pk = item.get("PK")
            if isinstance(pk, str) and pk.startswith("c#"):
                found[pk[2:]] = item
        keys = (response.get("UnprocessedKeys") or {}).get(table.name, {}).get("Keys") or []
        if not keys:
            break
        if attempt + 1 < BATCH_GET_ATTEMPTS:
            time.sleep(0.05 * 2**attempt)
    return found


def read_meetings(
    table, call_ids: List[str], projection: Optional[Dict[str, Any]] = None
) -> Tuple[Dict[str, Dict], int]:
    """Read the meeting rows of up to 100 CallIds, or the attributes ``projection`` names.

    Returns the rows by CallId and the number of meetings that could not be
    read. A batch read comes first; every id it did not return (a batch that
    failed, keys left unprocessed, a row that does not exist) is then read on
    its own. A row that is not there is skipped without being counted; a read
    that fails is counted.
    """
    rows, failed = read_meeting_rows(table, call_ids, projection)
    return rows, len(failed)


def read_meeting_rows(
    table, call_ids: List[str], projection: Optional[Dict[str, Any]] = None
) -> Tuple[Dict[str, Dict], List[str]]:
    """``read_meetings``, naming the ids that could not be read instead of counting them."""
    rows = get_meetings_by_ids(table, call_ids, projection)
    unreadable: List[str] = []
    for call_id in call_ids:
        if call_id in rows:
            continue
        try:
            key = {"PK": f"c#{call_id}", "SK": f"c#{call_id}"}
            item = table.get_item(Key=key, **(projection or {})).get("Item")
        except Exception as e:
            logger.warning("Error getting meeting %r (%s)", call_id, failure_class(e))
            unreadable.append(call_id)
            continue
        if item:
            rows[call_id] = item
    return rows, unreadable


# NOTE: `scan_recent_meetings` was removed.  The callers that used it now go
# through `walk_date_range` (GSI-backed); direct scans don't scale and are no
# longer acceptable once the tracking table grows.


def participant_in_meeting(meeting: Dict[str, Any], participant_name: str) -> bool:
    """Check if participant name is in meeting"""
    participants = meeting.get("Participants", [])

    # Participants may be list of names or list of dicts
    if not participants:
        return False

    # Handle list of strings
    if isinstance(participants[0], str):
        return any(participant_name.lower() in p.lower() for p in participants)

    # Handle list of dicts
    return any(
        participant_name.lower() in p.get("Name", "").lower()
        for p in participants
        if isinstance(p, dict)
    )


# What the summary step stores when it could not produce a summary, and its
# answer for a meeting with nothing to summarise (call_event_processor's
# FAILED_SUMMARY_TEXTS and EMPTY_SUMMARY_TEXT).
UNUSABLE_SUMMARIES = frozenset(
    {
        "",
        "an error occurred.",
        "an error occurred",
        "no summary available",
        "nothing to summarise: the meeting has no transcript",
    }
)


def summary_ready(meeting: Dict[str, Any]) -> bool:
    """Whether the meeting has a summary to read (get_meeting_summary)."""
    text = meeting.get("CallSummaryText")
    return isinstance(text, str) and text.strip().rstrip(".").lower() not in {
        entry.rstrip(".") for entry in UNUSABLE_SUMMARIES
    }


def format_meeting(meeting: Dict[str, Any], vp_id: Optional[str] = None) -> Dict[str, Any]:
    """Format meeting data for response.

    ``endTime`` is when an ended meeting last changed, which is its end unless it
    was changed afterwards; it is empty while the meeting runs.
    """
    meeting_id = meeting.get("CallId", meeting.get("PK", "").replace("c#", ""))
    ended = meeting.get("Status") == "ENDED"
    ready = summary_ready(meeting)
    out = {
        "meetingId": meeting_id,
        "meetingName": meeting.get("MeetingTopic", ""),
        "startTime": meeting.get("CreatedAt", ""),
        "endTime": meeting.get("UpdatedAt", "") if ended else None,
        "updatedAt": meeting.get("UpdatedAt", ""),
        "duration": meeting.get("TotalConversationDurationMillis", 0) / 1000,
        "status": meeting.get("Status", "UNKNOWN"),
        "participants": meeting.get("Participants", []),
        "owner": meeting.get("Owner", meeting.get("AgentId", "")),
        "labels": labels_of(meeting),
        "audience": meeting.get("Audience") or None,
        "summaryReady": ready,
        "hasSummary": ready or bool(meeting.get("Summary")),
        "hasTranscript": bool(meeting.get("TranscriptUri") or meeting.get("RecordingUrl")),
        "meetingUrl": get_meeting_url(meeting_id),
    }
    if vp_id:
        out["virtualParticipantId"] = vp_id
    out["virtualParticipantUrl"] = get_virtual_participant_url(vp_id) if vp_id else None
    return out
