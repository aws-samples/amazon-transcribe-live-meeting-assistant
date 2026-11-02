#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""Metering records: what a meeting used, recorded where the usage happened.

A metering record is one DynamoDB item in the meeting table (``EventSourcingTable``)
saying "this meeting used this much of this metered thing":

====================  =========================================================
Attribute             Meaning
====================  =========================================================
``PK``                ``mtr#<CallId>`` -- every record of one meeting shares it,
                      so the cost of a meeting is one ``Query``.
``SK``                ``<Context>#<RecordedAt>#<RecordId>``. Unique per record,
                      so writers never contend and a record is never updated.
``CallId``            The meeting.
``Context``           What the usage was *for*: ``Transcription``, ``Summary``,
                      ``MeetingAssistant``, ``VoiceAssistant`` ... (see
                      :data:`CONTEXTS`). The estimate is subtotalled by it.
``Service``           ``<service>/<api-or-model>``: ``transcribe/streaming``,
                      ``bedrock/<model id>``, ``fargate/x86_64`` ... The price
                      table is keyed on exactly this string.
``Usage``             Map of unit name to a number: ``inputTokens``,
                      ``seconds``, ``vcpuSeconds`` ... The price table prices
                      each unit separately.
``Details``           Optional map of strings for the reader: launch type,
                      session count, why a figure is approximate. Never priced.
``Region``            The region the usage was billed in, when it is not the
                      deployment's own (Nova Sonic can run elsewhere).
``Source``            The component that wrote the record.
``RecordedAt``        ISO-8601 UTC.
``ExpiresAfter``      Epoch seconds; the table's TTL attribute. The same
                      retention as the meeting record, so a meeting's cost
                      expires with the meeting.
====================  =========================================================

The shape mirrors the GenAI IDP Accelerator's ``"<context>/<service>/<api>": {unit:
value}`` metering map, flattened into one item per event because LMA's usage
arrives from several processes over the life of a meeting rather than from one
workflow that can hold the map in memory.

The TypeScript writers in the WebSocket transcriber and the Virtual Participant,
and the copy vendored into the meeting-assist stack, write the same shape;
``lma-ai-stack/test/test_metering_record_contract.py`` keeps them in step.

Writing a record must never break the feature that incurred the usage, so
:meth:`MeteringWriter.record` logs and swallows every failure. The cost of a
meeting with a lost record is shown as lower than it was, which
docs/meeting-cost-estimate.md lists as a known limit; a summary that failed
because its metering write did would be a far worse trade.
"""

from __future__ import annotations

import logging
import math
import os
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Dict, Mapping, Optional

import boto3
from lma_config import RETENTION, read_setting

LOGGER = logging.getLogger(__name__)

#: Partition-key prefix shared by every metering record. Writers' IAM grants are
#: conditioned on it (``dynamodb:LeadingKeys``), so a writer can add metering
#: records and nothing else.
PK_PREFIX = "mtr#"

#: Environment variable naming the table the records go in.
TABLE_ENV_VAR = "METERING_TABLE_NAME"

#: Environment variable holding the meeting retention in days, which the records
#: share. Fractional days are allowed, as for ``MeetingRecordExpirationInDays``.
EXPIRATION_ENV_VAR = "METERING_EXPIRATION_DAYS"

#: The contexts the estimate knows, in the order it lists their lines and subtotals.
#: A writer may use another name; it is listed after these, alphabetically.
CONTEXTS = (
    "Transcription",
    "VirtualParticipant",
    "VoiceAssistant",
    "MeetingAssistant",
    "Summary",
    "SpeakerNaming",
    "AgendaDerivation",
    "SlideText",
    "ConsentVerification",
    "Sentiment",
    "PageAutomation",
)

#: The numeric members of a Bedrock Converse ``usage`` block that are billed, in
#: the unit names the price table uses. ``totalTokens`` is deliberately not
#: kept: it is the sum of two of these, and pricing it would count them twice.
BEDROCK_TOKEN_UNITS = (
    "inputTokens",
    "outputTokens",
    "cacheReadInputTokens",
    "cacheWriteInputTokens",
)


def metering_pk(call_id: str) -> str:
    """The partition key holding every metering record of ``call_id``."""
    return f"{PK_PREFIX}{call_id}"


def bedrock_service(model_id: str) -> str:
    """The ``Service`` string for a Bedrock model or inference profile."""
    return f"bedrock/{model_id}"


def bedrock_usage(usage: Optional[Mapping[str, Any]], *, requests: int = 1) -> Dict[str, float]:
    """The billable units of a Bedrock ``usage`` block, plus a request count.

    Accepts the Converse API's camelCase block and the snake_case one some SDK
    wrappers return. Members that are absent, zero or not numbers are left out,
    so a record carries only what was actually used.
    """
    usage = usage or {}
    out: Dict[str, float] = {}
    for unit in BEDROCK_TOKEN_UNITS:
        snake = "".join(f"_{c.lower()}" if c.isupper() else c for c in unit)
        value = usage.get(unit, usage.get(snake))
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if value > 0:
            out[unit] = value
    if requests:
        out["requests"] = requests
    return out


def comprehend_units(text: str) -> int:
    """Billable Amazon Comprehend units for one request: 100 characters per unit,
    with a three-unit minimum per request."""
    return max(3, math.ceil(len(text or "") / 100))


def _expires_after(now_epoch: float, days: Optional[float]) -> Optional[int]:
    if days is None or days <= 0:
        return None
    return int(now_epoch + days * 86400)


def _env_days() -> Optional[float]:
    """Days a record written now is kept, or None for no expiry.

    The meeting retention: the Retention section's value on the Configuration
    page, at most the MeetingRecordExpirationInDays stack parameter's, which this
    function or task carries as ``METERING_EXPIRATION_DAYS``. Read on the
    section's standard tier, so a saved change reaches records written about five
    minutes later; with no configuration table named, the variable alone answers,
    and with neither a record gets no expiry.
    """
    raw = os.environ.get(EXPIRATION_ENV_VAR, "").strip()
    if raw:
        try:
            float(raw)
        except ValueError:
            LOGGER.warning(
                "%s=%r is not a number; metering records get no TTL", EXPIRATION_ENV_VAR, raw
            )
            return None
    try:
        days = read_setting(
            RETENTION.name, "meetingRecordExpirationInDays", env_var=EXPIRATION_ENV_VAR
        )
    except Exception:  # pylint: disable=broad-except -- a metering write never throws
        LOGGER.warning(
            "could not read the meeting retention; using %s", EXPIRATION_ENV_VAR, exc_info=True
        )
        return float(raw) if raw else None
    return float(days) if days is not None else None


def _number(value: float) -> Dict[str, str]:
    """A DynamoDB number. Rounded to 6 decimal places: no unit needs more, and a
    float's repr can carry noise DynamoDB would reject as too precise."""
    return {"N": format(Decimal(str(round(float(value), 6))).normalize(), "f")}


def build_item(
    call_id: str,
    context: str,
    service: str,
    usage: Mapping[str, float],
    *,
    source: str,
    details: Optional[Mapping[str, Any]] = None,
    region: Optional[str] = None,
    expiration_days: Optional[float] = None,
    record_id: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """The low-level DynamoDB item for one metering record.

    Raises:
        ValueError: ``call_id``, ``context`` or ``service`` is empty, or
            ``usage`` holds nothing numeric -- a record that says nothing is
            refused rather than stored.
    """
    if not call_id or not context or not service:
        raise ValueError("a metering record needs a CallId, a Context and a Service")
    clean = {
        unit: float(value)
        for unit, value in (usage or {}).items()
        if not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
        and value >= 0
    }
    if not clean:
        raise ValueError(f"the metering record for {service} carries no usage")
    moment = now or datetime.now(timezone.utc)
    recorded_at = moment.astimezone(timezone.utc).isoformat(timespec="milliseconds")
    recorded_at = recorded_at.replace("+00:00", "Z")
    rid = record_id or uuid.uuid4().hex
    item: Dict[str, Any] = {
        "PK": {"S": metering_pk(call_id)},
        "SK": {"S": f"{context}#{recorded_at}#{rid}"},
        "CallId": {"S": call_id},
        "Context": {"S": context},
        "Service": {"S": service},
        "Usage": {"M": {unit: _number(value) for unit, value in clean.items()}},
        "Source": {"S": source},
        "RecordedAt": {"S": recorded_at},
    }
    if details:
        item["Details"] = {
            "M": {str(k): {"S": str(v)} for k, v in details.items() if v is not None}
        }
    if region:
        item["Region"] = {"S": region}
    days = expiration_days if expiration_days is not None else _env_days()
    expires = _expires_after(moment.timestamp(), days)
    if expires is not None:
        item["ExpiresAfter"] = {"N": str(expires)}
    return item


class MeteringWriter:
    """Writes metering records. Build one per Lambda container at module scope.

    A writer with no table name is inert: :meth:`record` logs once and returns
    ``False``. That is what lets the metering calls sit unconditionally in a
    handler whose deployment, or test, does not configure a table.
    """

    def __init__(
        self,
        table_name: Optional[str] = None,
        *,
        source: str,
        client: Any = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._table_name = (
            table_name if table_name is not None else os.environ.get(TABLE_ENV_VAR, "")
        )
        self._source = source
        self._client = client
        self._clock = clock
        self._warned_unconfigured = False

    @property
    def enabled(self) -> bool:
        return bool(self._table_name)

    def _dynamodb(self) -> Any:
        if self._client is None:
            self._client = boto3.client("dynamodb")
        return self._client

    def record(
        self,
        call_id: Optional[str],
        context: str,
        service: str,
        usage: Mapping[str, float],
        *,
        details: Optional[Mapping[str, Any]] = None,
        region: Optional[str] = None,
    ) -> bool:
        """Store one record. Returns whether it was stored; never raises."""
        if not self._table_name:
            if not self._warned_unconfigured:
                LOGGER.info("%s is unset; metering records are not written", TABLE_ENV_VAR)
                self._warned_unconfigured = True
            return False
        if not call_id:
            LOGGER.warning("not metering %s for %s: no CallId", service, context)
            return False
        try:
            item = build_item(
                call_id,
                context,
                service,
                usage,
                source=self._source,
                details=details,
                region=region,
                now=datetime.fromtimestamp(self._clock(), timezone.utc),
            )
        except ValueError as error:
            LOGGER.info("not metering %s for %s: %s", service, context, error)
            return False
        try:
            self._dynamodb().put_item(TableName=self._table_name, Item=item)
        except Exception:  # noqa: BLE001 -- see the module docstring
            LOGGER.warning(
                "could not write the metering record for %s/%s on %s",
                context,
                service,
                call_id,
                exc_info=True,
            )
            return False
        return True

    def add(
        self,
        call_id: Optional[str],
        context: str,
        service: str,
        usage: Mapping[str, float],
    ) -> bool:
        """Add to the meeting's running counter for ``context``/``service``.

        For usage that arrives as many tiny increments -- one Comprehend call per
        transcript segment -- where a record per increment would be hundreds of
        items per meeting. The counter is one item with a fixed sort key
        (``<Context>#counter#<Service>``), and each unit is a top-level number
        attribute named ``u_<unit>`` because DynamoDB's ``ADD`` cannot create a
        member inside a map that does not exist yet. :func:`counter_usage` reads
        them back.

        ``ADD`` is not idempotent: a redelivered event adds its usage again.
        That over-counts by the size of a redelivery, which for the per-segment
        usage this is for is a fraction of a cent, and the estimate is labelled
        as one.
        """
        if not self._table_name or not call_id:
            return False
        clean = {
            unit: float(value)
            for unit, value in (usage or {}).items()
            if not isinstance(value, bool)
            and isinstance(value, (int, float))
            and math.isfinite(value)
            and value > 0
        }
        if not clean:
            return False
        now = datetime.fromtimestamp(self._clock(), timezone.utc)
        names = {"#ctx": "Context", "#svc": "Service", "#src": "Source", "#at": "RecordedAt"}
        values: Dict[str, Any] = {
            ":ctx": {"S": context},
            ":svc": {"S": service},
            ":src": {"S": self._source},
            ":at": {"S": now.isoformat(timespec="milliseconds").replace("+00:00", "Z")},
            ":cid": {"S": call_id},
        }
        sets = ["#ctx = :ctx", "#svc = :svc", "#src = :src", "#at = :at", "CallId = :cid"]
        expires = _expires_after(now.timestamp(), _env_days())
        if expires is not None:
            sets.append("ExpiresAfter = if_not_exists(ExpiresAfter, :exp)")
            values[":exp"] = {"N": str(expires)}
        adds = []
        for index, (unit, value) in enumerate(sorted(clean.items())):
            names[f"#u{index}"] = f"{COUNTER_UNIT_PREFIX}{unit}"
            values[f":u{index}"] = _number(value)
            adds.append(f"#u{index} :u{index}")
        try:
            self._dynamodb().update_item(
                TableName=self._table_name,
                Key={
                    "PK": {"S": metering_pk(call_id)},
                    "SK": {"S": f"{context}#counter#{service}"},
                },
                UpdateExpression=f"SET {', '.join(sets)} ADD {', '.join(adds)}",
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
            )
        except Exception:  # noqa: BLE001 -- see the module docstring
            LOGGER.warning(
                "could not add to the metering counter for %s/%s on %s",
                context,
                service,
                call_id,
                exc_info=True,
            )
            return False
        return True


#: Prefix of the top-level unit attributes on a counter item; see
#: :meth:`MeteringWriter.add`.
COUNTER_UNIT_PREFIX = "u_"


def counter_usage(record: Mapping[str, Any]) -> Dict[str, float]:
    """A record's usage: its ``Usage`` map plus any counter attributes."""
    usage: Dict[str, float] = {}
    for unit, value in (record.get("Usage") or {}).items():
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            usage[unit] = usage.get(unit, 0.0) + float(value)
    for key, value in record.items():
        if (
            key.startswith(COUNTER_UNIT_PREFIX)
            and isinstance(value, (int, float))
            and not isinstance(value, bool)
        ):
            unit = key[len(COUNTER_UNIT_PREFIX) :]
            usage[unit] = usage.get(unit, 0.0) + float(value)
    return usage
