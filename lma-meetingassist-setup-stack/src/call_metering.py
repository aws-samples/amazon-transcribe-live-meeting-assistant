# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Metering records for the meeting cost estimate, written from this stack.

The canonical writer is ``lma_metering.records`` in the AI stack's
configuration layer. That layer is not attached to the functions in this stack,
so the small part of it the meeting assistant needs is repeated here: one
DynamoDB item per answer, under ``PK = mtr#<CallId>``, carrying the model's
token usage. ``lma-ai-stack/test/test_metering_record_contract.py`` asserts the
two produce the same item, so they cannot drift apart silently.

Writing never raises: a lost record makes a meeting's estimate lower than it
was, which docs/meeting-cost-estimate.md lists as a known limit; an answer that failed because its
metering write did would be far worse.
"""

from __future__ import annotations

import logging
import math
import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Mapping, Optional

import boto3

logger = logging.getLogger(__name__)

PK_PREFIX = "mtr#"
TABLE_ENV_VAR = "METERING_TABLE_NAME"
EXPIRATION_ENV_VAR = "METERING_EXPIRATION_DAYS"
BEDROCK_TOKEN_UNITS = (
    "inputTokens",
    "outputTokens",
    "cacheReadInputTokens",
    "cacheWriteInputTokens",
)


def bedrock_usage(usage: Any, *, requests: int = 1) -> Dict[str, float]:
    """The billable token units of a usage block (dict or object), plus requests."""
    if usage is None:
        usage = {}
    get = (
        usage.get
        if isinstance(usage, Mapping)
        else (lambda key, default=None: getattr(usage, key, default))
    )
    out: Dict[str, float] = {}
    for unit in BEDROCK_TOKEN_UNITS:
        snake = "".join(f"_{c.lower()}" if c.isupper() else c for c in unit)
        value = get(unit, None)
        if value is None:
            value = get(snake, None)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if value > 0:
            out[unit] = value
    if requests:
        out["requests"] = requests
    return out


def _number(value: float) -> Dict[str, str]:
    return {"N": format(Decimal(str(round(float(value), 6))).normalize(), "f")}


def build_item(
    call_id: str,
    context: str,
    service: str,
    usage: Mapping[str, float],
    *,
    source: str,
    expiration_days: Optional[float] = None,
    record_id: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """The DynamoDB item, in the shape ``lma_metering.records.build_item`` writes."""
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
    item: Dict[str, Any] = {
        "PK": {"S": f"{PK_PREFIX}{call_id}"},
        "SK": {"S": f"{context}#{recorded_at}#{record_id or uuid.uuid4().hex}"},
        "CallId": {"S": call_id},
        "Context": {"S": context},
        "Service": {"S": service},
        "Usage": {"M": {unit: _number(value) for unit, value in clean.items()}},
        "Source": {"S": source},
        "RecordedAt": {"S": recorded_at},
    }
    if expiration_days is None:
        expiration_days = _retention_days()
    if expiration_days and expiration_days > 0:
        item["ExpiresAfter"] = {"N": str(int(moment.timestamp() + expiration_days * 86400))}
    return item


def _retention_days() -> Optional[float]:
    """Days a record written now is kept, or None for no expiry.

    The Retention section's meeting retention on the Configuration page, at most
    the MeetingRecordExpirationInDays stack parameter's value, which this function
    carries as METERING_EXPIRATION_DAYS (lma_metering.records reads it the same
    way). Deployed on its own, without the configuration layer, the variable alone.
    """
    # Imported here rather than at the top, so this module still loads on its
    # own, as the AI stack's contract test loads it beside lma_metering.
    # pylint: disable-next=import-outside-toplevel
    from configuration_settings import read_setting

    raw = os.environ.get(EXPIRATION_ENV_VAR, "").strip()
    try:
        if raw:
            float(raw)
        days = read_setting(
            "retention", "meetingRecordExpirationInDays", env_var=EXPIRATION_ENV_VAR
        )
        return float(days) if days not in (None, "") else None
    except ValueError:
        return None


_client = None


def record(
    call_id: Optional[str], context: str, service: str, usage: Mapping[str, float], *, source: str
) -> bool:
    """Store one metering record. Returns whether it was stored; never raises."""
    global _client  # pylint: disable=global-statement
    table = os.environ.get(TABLE_ENV_VAR, "")
    if not table or not call_id:
        return False
    try:
        item = build_item(call_id, context, service, usage, source=source)
    except ValueError as error:
        logger.info("not metering %s for %s: %s", service, context, error)
        return False
    try:
        if _client is None:
            _client = boto3.client("dynamodb")
        _client.put_item(TableName=table, Item=item)
    except Exception:  # noqa: BLE001 -- see the module docstring
        logger.warning(
            "could not write the metering record for %s/%s", context, service, exc_info=True
        )
        return False
    return True
