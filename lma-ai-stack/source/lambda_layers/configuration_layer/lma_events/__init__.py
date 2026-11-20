# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""Publish meeting lifecycle events to the stack's own EventBridge event bus.

Four events, each with ``source`` ``lma``:

* ``Meeting Started`` -- a meeting record is created, or a reopened meeting
  starts a later capture session;
* ``Meeting Ended`` -- a capture session's END closes the meeting;
* ``Summary Ready`` -- a generated or regenerated summary is stored;
* ``Virtual Participant Status Changed`` -- a Virtual Participant's status moves.

The ``detail`` carries identifiers and states only (``DETAIL_FIELDS``): never a
transcript, a summary, an agenda, a meeting name, the meeting context, the
checklist or anybody's name. ``build_detail`` refuses any other field, and drops
a field that is unknown or does not apply rather than sending it empty. See
docs/meeting-events.md for the contract.

``publish`` never raises. A meeting must not fail, or be redelivered, because
the bus could not be reached: a failure is logged with the detail-type and the
identifiers, and counted in the ``MeetingEventsPublishFailed`` metric. When
``MEETING_EVENT_BUS_NAME`` is empty (the stack parameter ``EnableMeetingEvents``
is ``false``) it does nothing at all.

Like the rest of this layer it uses only the standard library and the boto3
that ships with the Lambda runtime.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, Mapping, Optional

LOGGER = logging.getLogger(__name__)

SOURCE = "lma"
VERSION = 1

MEETING_STARTED = "Meeting Started"
MEETING_ENDED = "Meeting Ended"
SUMMARY_READY = "Summary Ready"
VP_STATUS_CHANGED = "Virtual Participant Status Changed"
DETAIL_TYPES = (MEETING_STARTED, MEETING_ENDED, SUMMARY_READY, VP_STATUS_CHANGED)

#: Every field an event's ``detail`` may carry, in the order they are written.
#: Identifiers and states only; anything else is refused by ``build_detail``.
DETAIL_FIELDS = (
    "version",
    "meetingId",
    "virtualParticipantId",
    "owner",
    "labels",
    "audience",
    "status",
    "summaryProfile",
    "sessionNumber",
    "vpStatus",
    "manualActionType",
    "occurredAt",
)

BUS_NAME_ENV_VAR = "MEETING_EVENT_BUS_NAME"
# The same variable, and so the same namespace, as every other metric LMA
# publishes (metrics_utils.catalogue.NAMESPACE_ENV_VAR in the transcript
# enrichment layer, which this layer cannot import).
NAMESPACE_ENV_VAR = "METRIC_NAMESPACE"
PUBLISHED_METRIC = "MeetingEventsPublished"
PUBLISH_FAILED_METRIC = "MeetingEventsPublishFailed"

# Short, so that a slow EventBridge endpoint cannot hold up the Kinesis shard
# the call event processor is reading: three attempts at these timeouts is the
# most one event can cost.
CONNECT_TIMEOUT_SECONDS = 2
READ_TIMEOUT_SECONDS = 3
MAX_ATTEMPTS = 3

Sink = Callable[[str], None]

_CLIENT: Any = None
# The call event processor publishes from its event loop's thread pool, and
# creating a client from boto3's default session is not safe from two threads
# at once.
_CLIENT_LOCK = threading.Lock()


def bus_name() -> str:
    """The bus to publish to; empty when meeting events are switched off."""
    return os.environ.get(BUS_NAME_ENV_VAR, "").strip()


def enabled() -> bool:
    """Whether this deployment publishes meeting events."""
    return bool(bus_name())


def _client() -> Any:
    with _CLIENT_LOCK:
        return _CLIENT if _CLIENT is not None else _new_client()


def _new_client() -> Any:
    global _CLIENT  # pylint: disable=global-statement
    if _CLIENT is None:
        # pylint: disable=import-outside-toplevel
        import boto3
        from botocore.config import Config

        _CLIENT = boto3.client(
            "events",
            config=Config(
                connect_timeout=CONNECT_TIMEOUT_SECONDS,
                read_timeout=READ_TIMEOUT_SECONDS,
                retries={"mode": "adaptive", "max_attempts": MAX_ATTEMPTS},
            ),
        )
    return _CLIENT


def iso_utc(value: Any) -> Optional[str]:
    """An ISO 8601 UTC time ending in ``Z``, or None when ``value`` is not one.

    Accepts a datetime, an ISO 8601 string (``Z`` or an offset) or epoch seconds.
    """
    if value is None or value == "":
        return None
    moment: Optional[datetime] = None
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        moment = datetime.fromtimestamp(float(value), tz=timezone.utc)
    elif isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            moment = datetime.fromisoformat(text)
        except ValueError:
            return None
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    moment = moment.astimezone(timezone.utc)
    stamp = moment.isoformat(timespec="milliseconds")
    return stamp.replace("+00:00", "Z")


def labels_map(labels: Any) -> Optional[Dict[str, str]]:
    """A meeting's labels as ``{key: value}``.

    Accepts the stored shape (a list of ``{"Key", "Value"}``) or a mapping. None
    when there are none, so the field is left out.
    """
    out: Dict[str, str] = {}
    if isinstance(labels, Mapping):
        items: Iterable[Any] = [{"Key": k, "Value": v} for k, v in labels.items()]
    elif isinstance(labels, (list, tuple)):
        items = labels
    else:
        return None
    for item in items:
        if not isinstance(item, Mapping):
            continue
        key = item.get("Key", item.get("key"))
        value = item.get("Value", item.get("value"))
        if isinstance(key, str) and key and isinstance(value, str):
            out[key] = value
    return out or None


def _absent(value: Any) -> bool:
    return value is None or value == "" or (isinstance(value, (dict, list)) and not value)


def build_detail(**fields: Any) -> Dict[str, Any]:
    """An event's ``detail``: ``version`` plus the given fields that are known.

    A field outside ``DETAIL_FIELDS`` raises ValueError, so meeting content
    cannot be added to an event by mistake. A field that is None, empty, or an
    empty collection is left out.
    """
    unknown = sorted(set(fields) - set(DETAIL_FIELDS))
    if unknown:
        raise ValueError(f"not a meeting event field: {', '.join(unknown)}")
    values = dict(fields, version=VERSION)
    if "occurredAt" in values:
        values["occurredAt"] = iso_utc(values["occurredAt"])
    if "sessionNumber" in values and values["sessionNumber"] is not None:
        try:
            values["sessionNumber"] = int(values["sessionNumber"])
        except (TypeError, ValueError):
            values["sessionNumber"] = None
    return {name: values[name] for name in DETAIL_FIELDS if not _absent(values.get(name))}


def meeting_detail(
    meeting: Mapping[str, Any],
    *,
    status: Optional[str] = None,
    session_number: Any = None,
    occurred_at: Any = None,
    virtual_participant_id: Optional[str] = None,
) -> Dict[str, Any]:
    """The ``detail`` of a meeting event, from a meeting record (a Call).

    ``status`` overrides the record's own; ``occurred_at`` defaults to the
    record's ``UpdatedAt``.
    """
    return build_detail(
        meetingId=meeting.get("CallId"),
        virtualParticipantId=virtual_participant_id,
        owner=meeting.get("Owner"),
        labels=labels_map(meeting.get("Labels")),
        audience=meeting.get("Audience"),
        status=status or meeting.get("Status"),
        summaryProfile=meeting.get("SummaryProfile"),
        sessionNumber=session_number,
        occurredAt=occurred_at if occurred_at is not None else meeting.get("UpdatedAt"),
    )


def summary_detail(meeting: Mapping[str, Any]) -> Dict[str, Any]:
    """The ``detail`` of Summary Ready, from the meeting record as stored.

    ``occurredAt`` is when the summary was stored, from ``CallSummaryState``'s
    ``updatedAt``. Not the meeting's ``UpdatedAt``: a regeneration writes the
    summary without moving it, so a regenerated summary would otherwise carry
    the same time as the first one and be dropped by a consumer's deduplication.
    """
    stored_at = None
    raw = meeting.get("CallSummaryState")
    if isinstance(raw, str) and raw:
        try:
            state = json.loads(raw)
        except ValueError:
            state = None
        if isinstance(state, Mapping):
            stored_at = iso_utc(state.get("updatedAt"))
    return meeting_detail(meeting, occurred_at=stored_at)


def vp_detail(row: Mapping[str, Any], occurred_at: Any = None) -> Dict[str, Any]:
    """The ``detail`` of a Virtual Participant status event, from its table row.

    ``meetingId`` is the row's ``CallId``, left out until the Virtual
    Participant has created its meeting. ``manualActionType`` is sent only
    while the status is ``MANUAL_ACTION_REQUIRED``.
    """
    status = row.get("status")
    manual_action = row.get("manualActionType") if status == "MANUAL_ACTION_REQUIRED" else None
    return build_detail(
        meetingId=row.get("CallId"),
        virtualParticipantId=row.get("id"),
        owner=row.get("Owner") or row.get("owner"),
        labels=labels_map(row.get("labels")),
        audience=row.get("audience"),
        summaryProfile=row.get("summaryProfile"),
        vpStatus=status,
        manualActionType=manual_action,
        occurredAt=occurred_at if occurred_at is not None else row.get("updatedAt"),
    )


def _emit_metric(name: str, sink: Sink) -> None:
    """One Embedded Metric Format record, in the namespace every LMA metric uses.

    No dimensions, like the rest of LMA's metrics, so the dashboard and the alarm
    read one series per stack. Never raises.
    """
    namespace = os.environ.get(NAMESPACE_ENV_VAR, "").strip()
    if not namespace:
        return
    try:
        sink(
            json.dumps(
                {
                    "_aws": {
                        "Timestamp": int(time.time() * 1000),
                        "CloudWatchMetrics": [
                            {
                                "Namespace": namespace,
                                "Dimensions": [[]],
                                "Metrics": [{"Name": name, "Unit": "Count"}],
                            }
                        ],
                    },
                    name: 1,
                }
            )
        )
    except Exception:  # pylint: disable=broad-except
        LOGGER.warning("could not write the %s metric record", name)


def publish(
    detail_type: str,
    detail: Mapping[str, Any],
    *,
    client: Any = None,
    sink: Sink = print,
) -> bool:
    """Put one event on the meeting event bus. Never raises.

    Returns True when EventBridge accepted it, False when it did not or when
    meeting events are switched off. ``client`` and ``sink`` are for tests.
    """
    name = bus_name()
    if not name:
        return False
    ids = {
        "detailType": detail_type,
        "meetingId": detail.get("meetingId"),
        "virtualParticipantId": detail.get("virtualParticipantId"),
    }
    try:
        if detail_type not in DETAIL_TYPES:
            raise ValueError(f"not a meeting event detail-type: {detail_type}")
        entry: Dict[str, Any] = {
            "Source": SOURCE,
            "DetailType": detail_type,
            "Detail": json.dumps(dict(detail), separators=(",", ":")),
            "EventBusName": name,
        }
        occurred = detail.get("occurredAt")
        if occurred:
            entry["Time"] = datetime.fromisoformat(str(occurred).replace("Z", "+00:00"))
        response = (client or _client()).put_events(Entries=[entry])
        if response.get("FailedEntryCount"):
            failed = (response.get("Entries") or [{}])[0]
            LOGGER.error(
                "meeting event not accepted by the event bus: %s errorCode=%s",
                ids,
                failed.get("ErrorCode"),
            )
            _emit_metric(PUBLISH_FAILED_METRIC, sink)
            return False
    except Exception as error:  # pylint: disable=broad-except
        # The error's type and code only: a botocore message can quote the
        # request, and the request is not something to copy into a log.
        code = getattr(error, "response", {}).get("Error", {}).get("Code")
        LOGGER.error(
            "could not publish a meeting event: %s error=%s code=%s",
            ids,
            type(error).__name__,
            code,
        )
        _emit_metric(PUBLISH_FAILED_METRIC, sink)
        return False
    LOGGER.info("published a meeting event: %s", ids)
    _emit_metric(PUBLISHED_METRIC, sink)
    return True


# The in-app notification that goes with Summary Ready. A separate mechanism
# from the bus (docs/meeting-notifications.md): it reaches the people who can see
# the meeting on its page, whether or not anything subscribes to the bus, and it
# is not switched off with EnableMeetingEvents. Muting the category is how a
# user turns it off.
NOTIFICATION_CATEGORY = "meeting"
SUMMARY_READY_NOTIFICATION_TYPE = "summary_ready"
SUMMARY_READY_NOTIFICATION_TITLE = "The meeting summary is ready"
# One notification per meeting: a regenerated summary updates it in place.
SUMMARY_READY_DEDUP_KEY = "summary-ready"
# Short, like the bus's own timeouts: the notification is published from the
# call event processor's Kinesis path as well as from the regenerator.
NOTIFICATION_TIMEOUT_SECONDS = 3.0


def notify_summary_ready(
    call_id: Optional[str],
    *,
    regenerated: bool = False,
    graphql: Any = None,
    timeout: float = NOTIFICATION_TIMEOUT_SECONDS,
) -> bool:
    """Publish the meeting's ``summary_ready`` notification. Never raises.

    Carries no summary text: the title only, and the meeting page shows the
    summary itself. ``timeout`` bounds the request, in seconds.
    """
    if not call_id:
        return False
    try:
        # pylint: disable-next=import-outside-toplevel
        from lma_notifications import publish_meeting_notification

        publish_meeting_notification(
            call_id,
            category=NOTIFICATION_CATEGORY,
            type=SUMMARY_READY_NOTIFICATION_TYPE,
            title=(
                "The meeting summary was regenerated"
                if regenerated
                else SUMMARY_READY_NOTIFICATION_TITLE
            ),
            source="lma",
            dedup_key=SUMMARY_READY_DEDUP_KEY,
            graphql=graphql,
            timeout=timeout,
        )
        return True
    except Exception as error:  # pylint: disable=broad-except
        LOGGER.warning(
            "could not publish the summary-ready notification: callId=%s error=%s",
            call_id,
            type(error).__name__,
        )
        return False
