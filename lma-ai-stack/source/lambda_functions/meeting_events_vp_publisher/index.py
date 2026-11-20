#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""Virtual Participant Status Changed, from the Virtual Participant table's stream.

Every writer of a Virtual Participant's status writes the one table: the
container, its entrypoint, both launch paths (the state machine and the
scheduler Lambda), the task reaper, the MicroVM launcher, the manager Lambda and
the AppSync resolvers. Reading the table's stream therefore sees each status
change whoever made it, without any of them publishing for itself.

A record is published when the row's ``status`` differs from the one before it;
a new row counts as a change from no status. Every other write -- a join
sub-step, the VNC endpoint, the CallId link -- publishes nothing.

Never fails its batch. An event that cannot be published is logged and counted
by ``lma_events``; failing the batch would only retry the same records, and
retrying would not make the bus reachable sooner.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional

import lma_events  # pylint: disable=import-error  # from the configuration layer
from boto3.dynamodb.types import TypeDeserializer

LOGGER = logging.getLogger()
LOGGER.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

_DESERIALIZER = TypeDeserializer()


def _image(record: Mapping[str, Any], name: str) -> Dict[str, Any]:
    raw = (record.get("dynamodb") or {}).get(name) or {}
    return {key: _DESERIALIZER.deserialize(value) for key, value in raw.items()}


def _approximate_time(record: Mapping[str, Any]) -> Optional[str]:
    stamp = (record.get("dynamodb") or {}).get("ApproximateCreationDateTime")
    if stamp is None:
        return None
    return lma_events.iso_utc(datetime.fromtimestamp(float(stamp), tz=timezone.utc))


def status_change(record: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """The event detail for one stream record, or None when its status did not move."""
    if record.get("eventName") not in ("INSERT", "MODIFY"):
        return None
    new = _image(record, "NewImage")
    status = new.get("status")
    if not isinstance(status, str) or not status:
        return None
    if _image(record, "OldImage").get("status") == status:
        return None
    # The row's own updatedAt when it has one, so the event's time is the one
    # the record holds; the stream's own timestamp otherwise. Both are the same
    # on a redelivery of the record.
    occurred_at = new.get("updatedAt") or _approximate_time(record)
    return lma_events.vp_detail(new, occurred_at=occurred_at)


def handler(event: Mapping[str, Any], _context: Any) -> Dict[str, Any]:
    """Publish one event per status change in the batch."""
    published = 0
    for record in event.get("Records") or []:
        try:
            detail = status_change(record)
        except Exception as error:  # pylint: disable=broad-except
            LOGGER.error(
                "could not read a Virtual Participant stream record: %s", type(error).__name__
            )
            continue
        if detail and lma_events.publish(lma_events.VP_STATUS_CHANGED, detail):
            published += 1
    return {"published": published}
