#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""Turning a meeting's metering records into an itemised cost estimate.

The price table is the ``call_pricing`` section of the configuration store,
seeded from ``configuration_seeder/defaults/CallPricing.json``. Its shape:

.. code-block:: json

    {
      "currency": "USD",
      "pricesAsOf": "2026-10-28",
      "source": "...",
      "baseRegion": "us-east-1",
      "pricedRegions": ["us-east-1", "us-west-2"],
      "regions": {
        "default":   {"transcribe/streaming": {"seconds": 0.0001667}, ...},
        "us-west-2": {"bedrock/amazon.nova-2-sonic-v1:0": {"textInputTokens": 3.19e-7}}
      },
      "nonChargeableUnits": ["requests", "sessions", ...],
      "notes": {"ec2/virtual-participant-host": "..."},
      "notIncluded": ["..."]
    }

Prices are US dollars per single unit -- per token, per second, per vCPU-second.
A region's prices are ``regions.default`` with ``regions.<region>`` merged over
it, so a region entry lists only what differs from the base.

The lookup rule is the IDP Accelerator's, deliberately strict: a record's
``Service`` must match a price entry **exactly** and each unit must be named in
it. Nothing is guessed from a similar-looking entry, because a plausible wrong
price is worse than a visible gap. A unit with no price is reported as
*unpriced* and left out of every total, and the count of unpriced lines travels
with the result so a reader of the estimate can tell the total is incomplete.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from boto3.dynamodb.types import TypeDeserializer

# The merge the configuration store applies between baseline and profile, used
# for the region overlay too so the two layers cannot come to merge differently.
from lma_config.store import deep_merge

from .records import CONTEXTS, counter_usage, metering_pk

_DESERIALIZER = TypeDeserializer()

#: Units that are counted for the reader but never billed on their own, used
#: when the price table does not name its own list.
#: ``images`` counts the pictures the meeting assistant sent a model; they are
#: billed as the model's input tokens, which the same answer's record carries.
DEFAULT_NON_CHARGEABLE_UNITS = ("requests", "sessions", "audioSeconds", "runtimeSeconds", "images")


def prices_for_region(pricing: Mapping[str, Any], region: str) -> Tuple[Dict[str, Any], bool]:
    """The price entries in force for ``region``, and whether they are its own.

    A region has prices of its own when it is listed in ``pricedRegions`` or has
    a ``regions.<region>`` entry; either is enough. Otherwise the base region's
    prices are returned, the second value is ``False``, and the estimate says so.
    """
    regions = pricing.get("regions") or {}
    base = regions.get("default") or {}
    priced = set(pricing.get("pricedRegions") or [])
    own = region in priced or region in regions
    return deep_merge(base, regions.get(region) or {}), own


def _plain(value: Any) -> Any:
    """DynamoDB's Decimals to floats, recursively."""
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if hasattr(value, "as_tuple"):  # Decimal
        return float(value)
    return value


def read_records(client: Any, table_name: str, call_id: str) -> List[Dict[str, Any]]:
    """Every metering record of ``call_id``, as plain dicts."""
    records: List[Dict[str, Any]] = []
    kwargs: Dict[str, Any] = {
        "TableName": table_name,
        "KeyConditionExpression": "PK = :pk",
        "ExpressionAttributeValues": {":pk": {"S": metering_pk(call_id)}},
    }
    while True:
        response = client.query(**kwargs)
        for item in response.get("Items", []):
            records.append(_plain({k: _DESERIALIZER.deserialize(v) for k, v in item.items()}))
        last = response.get("LastEvaluatedKey")
        if not last:
            return records
        kwargs["ExclusiveStartKey"] = last


def aggregate(
    records: Iterable[Mapping[str, Any]], default_region: str
) -> Dict[Tuple[str, str, str], Dict[str, Any]]:
    """Sum the records' usage by (context, service, region).

    Each value is ``{"usage": {unit: total}, "records": n, "details": {...}}``.
    Details from several records are merged; where they disagree the values are
    joined, so a reader can see that two sessions ran on different launch types.
    """
    totals: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    for record in records:
        context = str(record.get("Context") or "Other")
        service = str(record.get("Service") or "")
        if not service:
            continue
        region = str(record.get("Region") or default_region)
        bucket = totals.setdefault(
            (context, service, region), {"usage": {}, "records": 0, "details": {}}
        )
        bucket["records"] += 1
        for unit, value in counter_usage(record).items():
            bucket["usage"][unit] = bucket["usage"].get(unit, 0.0) + value
        for key, value in (record.get("Details") or {}).items():
            seen = bucket["details"].get(key)
            text = str(value)
            if seen is None:
                bucket["details"][key] = text
            elif text not in seen.split(", "):
                bucket["details"][key] = f"{seen}, {text}"
    return totals


def _context_rank(context: str) -> Tuple[int, str]:
    try:
        return (CONTEXTS.index(context), context)
    except ValueError:
        return (len(CONTEXTS), context)


def estimate_call_cost(
    call_id: str,
    records: Iterable[Mapping[str, Any]],
    pricing: Mapping[str, Any],
    deployment_region: str,
    *,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """The itemised estimate for one meeting, in the ``CallCost`` GraphQL shape."""
    records = list(records)
    non_chargeable = set(pricing.get("nonChargeableUnits") or DEFAULT_NON_CHARGEABLE_UNITS)
    # Read defensively: a hand-repaired record could hold anything here, and an
    # unreadable note must cost the line its note rather than the whole estimate.
    notes_raw = pricing.get("notes")
    notes: Mapping[str, str] = notes_raw if isinstance(notes_raw, Mapping) else {}
    price_cache: Dict[str, Tuple[Dict[str, Any], bool]] = {}
    fallback_regions: set = set()

    items: List[Dict[str, Any]] = []
    context_totals: Dict[str, float] = {}
    total = 0.0
    unpriced = 0

    grouped = aggregate(records, deployment_region)
    for context, service, region in sorted(
        grouped, key=lambda key: (_context_rank(key[0]), key[1], key[2])
    ):
        bucket = grouped[(context, service, region)]
        if region not in price_cache:
            price_cache[region] = prices_for_region(pricing, region)
        prices, own = price_cache[region]
        if not own:
            fallback_regions.add(region)
        entry = prices.get(service)
        details = ", ".join(f"{k}: {v}" for k, v in sorted(bucket["details"].items()))
        for unit in sorted(bucket["usage"]):
            quantity = bucket["usage"][unit]
            line: Dict[str, Any] = {
                "Context": context,
                "Service": service,
                "Region": region,
                "Unit": unit,
                "Quantity": round(quantity, 6),
                "UnitPrice": None,
                "Cost": None,
                "Chargeable": unit not in non_chargeable,
                "Priced": False,
                "Note": notes.get(service) or None,
                "Details": details or None,
            }
            if unit in non_chargeable:
                line["Priced"] = True
                line["UnitPrice"] = 0.0
                line["Cost"] = 0.0
            elif isinstance(entry, Mapping) and isinstance(entry.get(unit), (int, float)):
                price = float(entry[unit])
                cost = quantity * price
                line.update(UnitPrice=price, Cost=round(cost, 8), Priced=True)
                total += cost
                context_totals[context] = context_totals.get(context, 0.0) + cost
            elif quantity == 0:
                line.update(UnitPrice=0.0, Cost=0.0, Priced=True)
            else:
                unpriced += 1
            items.append(line)

    moment = now or datetime.now(timezone.utc)
    return {
        "CallId": call_id,
        "Currency": pricing.get("currency", "USD"),
        "TotalCost": round(total, 6),
        "ContextTotals": [
            {"Context": c, "Cost": round(v, 6)}
            for c, v in sorted(context_totals.items(), key=lambda kv: _context_rank(kv[0]))
        ],
        "Items": items,
        "UnpricedCount": unpriced,
        "RecordCount": len(records),
        "PricesAsOf": pricing.get("pricesAsOf"),
        "PriceSource": pricing.get("source"),
        "Region": deployment_region,
        "BaseRegion": pricing.get("baseRegion"),
        "RegionsWithoutListedPrices": sorted(fallback_regions),
        "NotIncluded": list(pricing.get("notIncluded") or []),
        "ComputedAt": moment.astimezone(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
    }
