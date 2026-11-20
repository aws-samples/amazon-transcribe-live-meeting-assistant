# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""One periodic pass over a live meeting at a time: the run lease.

A feature that reads a meeting while it is in progress -- live guidance, the
whiteboards' assistant -- is queued by the call event processor from finished
speech segments, and several invocations can arrive close together. Each pass
keeps a state row of its own in the event-sourcing table (``<prefix><CallId>`` /
``<sort key>``), and a run starts only by one conditional write on it, which
holds when:

* no other run holds the lease (``LeaseUntil`` absent or past);
* the last run was at least ``interval_seconds`` ago, unless the run ignores the
  interval (the last run at a meeting's end); a caller that allows a shorter gap,
  such as a run on request, passes a smaller ``interval_seconds``;
* the meeting has used fewer than ``max_runs`` runs.

So of the invocations a busy stream produces, one runs and the others stop after
that write. The lease is longer than the function's timeout, so a run that is
stopped cannot hold its meeting for longer than the lease. ``release`` gives it
back, conditional on its own lease, so a run that outlived its lease cannot clear
another's; after a run that counts (``ran``) it adds one to ``RunCount``, sets
``LastRunAt``, and stores what the run leaves for the next one.

The state row takes an expiry once (``if_not_exists``), which a pass sets to its
content's: the transcript's, for passes that keep anything drawn from it.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Dict, List, Optional

from boto3.dynamodb.types import TypeDeserializer
from botocore.exceptions import ClientError

LOGGER = logging.getLogger(__name__)
_DESERIALIZER = TypeDeserializer()


def _plain(value: Any) -> Any:
    """DynamoDB numbers as int or float, for code and for JSON."""
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    return value


def _conflict(error: ClientError) -> bool:
    return error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


class LivePass:
    """The run lease of one pass, on its state row."""

    def __init__(  # pylint: disable=too-many-arguments
        self,
        client: Any,
        table: str,
        *,
        prefix: str,
        sort_key: str,
        lease_seconds: int,
        name: str = "pass",
    ) -> None:
        self.client = client
        self.table = table
        self.prefix = prefix
        self.sort_key = sort_key
        self.lease_seconds = int(lease_seconds)
        self.name = name

    def key(self, call_id: str) -> Dict[str, Any]:
        return {"PK": {"S": f"{self.prefix}{call_id}"}, "SK": {"S": self.sort_key}}

    def read(self, call_id: str) -> Dict[str, Any]:
        """The state row, or an empty one."""
        item = self.client.get_item(TableName=self.table, Key=self.key(call_id)).get("Item")
        if not item:
            return {}
        return _plain({k: _DESERIALIZER.deserialize(v) for k, v in item.items()})

    def claim(  # pylint: disable=too-many-arguments
        self,
        call_id: str,
        *,
        now: float,
        interval_seconds: float,
        max_runs: int,
        expires: Optional[int] = None,
        ignore_interval: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """Take the meeting's lease: the state row, or None when a run may not start now."""
        values: Dict[str, Any] = {
            ":lease": {"N": str(int(now) + self.lease_seconds)},
            ":now": {"N": str(now)},
            ":max": {"N": str(int(max_runs))},
            ":call": {"S": call_id},
        }
        sets = ["LeaseUntil = :lease", "CallId = :call"]
        if expires:
            values[":ttl"] = {"N": str(int(expires))}
            sets.append("ExpiresAfter = if_not_exists(ExpiresAfter, :ttl)")
        conditions = [
            "(attribute_not_exists(LeaseUntil) OR LeaseUntil < :now)",
            "(attribute_not_exists(RunCount) OR RunCount < :max)",
        ]
        if not ignore_interval:
            values[":due"] = {"N": str(now - interval_seconds)}
            conditions.append("(attribute_not_exists(LastRunAt) OR LastRunAt <= :due)")
        try:
            response = self.client.update_item(
                TableName=self.table,
                Key=self.key(call_id),
                UpdateExpression="SET " + ", ".join(sets),
                ConditionExpression=" AND ".join(conditions),
                ExpressionAttributeValues=values,
                ReturnValues="ALL_NEW",
            )
        except ClientError as error:
            if _conflict(error):
                return None
            raise
        attributes = response.get("Attributes") or {}
        return _plain({k: _DESERIALIZER.deserialize(v) for k, v in attributes.items()})

    def release(self, call_id: str, lease: Any, *, ran: bool, now: float, **fields: Any) -> None:
        """Give the lease back; after a run (``ran``), count it and store what it leaves.

        ``fields`` are stored as numbers (int, float) or text (anything else).
        """
        sets: List[str] = []
        values: Dict[str, Any] = {":lease": {"N": str(int(lease))}}
        names: Dict[str, str] = {}
        if ran:
            sets += ["LastRunAt = :now", "RunCount = if_not_exists(RunCount, :zero) + :one"]
            values.update({":now": {"N": str(now)}, ":zero": {"N": "0"}, ":one": {"N": "1"}})
        for number, (name, value) in enumerate(fields.items()):
            names[f"#f{number}"] = name
            values[f":f{number}"] = (
                {"N": str(value)}
                if isinstance(value, (int, float)) and not isinstance(value, bool)
                else {"S": str(value)}
            )
            sets.append(f"#f{number} = :f{number}")
        expression = ("SET " + ", ".join(sets) + " " if sets else "") + "REMOVE LeaseUntil"
        kwargs: Dict[str, Any] = {
            "TableName": self.table,
            "Key": self.key(call_id),
            "UpdateExpression": expression,
            "ConditionExpression": "LeaseUntil = :lease",
            "ExpressionAttributeValues": values,
        }
        if names:
            kwargs["ExpressionAttributeNames"] = names
        try:
            self.client.update_item(**kwargs)
        except ClientError as error:
            if not _conflict(error):
                raise
            LOGGER.warning("The %s lease for %s was no longer this run's", self.name, call_id)
