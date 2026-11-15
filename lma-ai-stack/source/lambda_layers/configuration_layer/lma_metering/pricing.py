#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""Turning a meeting's metering records into an itemised cost estimate.

The price table is the ``call_pricing`` section of the configuration store,
seeded at deploy time from ``configuration_seeder/defaults/CallPricing.json``.
Its shape:

.. code-block:: json

    {
      "currency": "USD",
      "pricesAsOf": "2026-11-09",
      "baseRegion": "us-east-1",
      "pricedRegions": ["us-east-1", "us-west-2", "eu-west-1", ...],
      "regions": {
        "default":   {"transcribe/streaming": {"seconds": 0.0001667}, ...},
        "eu-west-1": {"bedrock/eu.amazon.nova-lite-v1:0": {"inputTokens": 7.8e-8}}
      },
      "sources": {
        "default":   {"transcribe/streaming": {"basis": "price-list-api",
                      "offer": "transcribe", "usageType": "USE1-StreamingAudio",
                      "retrieved": "2026-11-09", "url": "https://..."}},
        "eu-west-1": {...}
      },
      "nonChargeableUnits": ["requests", "sessions", ...],
      "notes": {"ec2/virtual-participant-host": "..."},
      "notIncluded": ["..."]
    }

Prices are US dollars per single unit -- per token, per second, per vCPU-second.
A region's prices are ``regions.default`` with ``regions.<region>`` merged over
it, and its sources are ``sources.default`` with ``sources.<region>`` merged the
same way, so a source always sits beside the price it documents.

Every line of an estimate says where its price came from. A price equal to the
one the release ships is reported with that price's source. A price that differs
from it -- one an administrator set in the active configuration profile -- is
reported as the profile's, with the shipped price beside it, so a reader can tell
a negotiated rate from a list price and see both.

The lookup rule is the IDP Accelerator's, deliberately strict: a record's
``Service`` must match a price entry **exactly** and each unit must be named in
it. Nothing is guessed from a similar-looking entry, because a plausible wrong
price is worse than a visible gap. A unit with no price is reported as
*unpriced* and left out of every total, and the count of unpriced lines travels
with the result so a reader of the estimate can tell the total is incomplete.

One exception, for Amazon Bedrock models only, and always named on the line it
applies to: an id the table does not list for the record's region -- a geography
profile or plain id the Model inference geography setting produced -- is priced
as the same model under another id (see
:func:`lma_config.inference_geography.price_stand_ins`), and its line's note
says which. It is never priced as a different model.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from boto3.dynamodb.types import TypeDeserializer

# The merge the configuration store applies between baseline and profile, used
# for the region overlay too so the two layers cannot come to merge differently.
from lma_config.inference_geography import price_stand_ins
from lma_config.store import deep_merge

from .records import CONTEXTS, counter_usage, metering_pk

_DESERIALIZER = TypeDeserializer()

#: Units that are counted for the reader but never billed on their own, used
#: when the price table does not name its own list.
#: ``images`` counts the pictures the meeting assistant sent a model; they are
#: billed as the model's input tokens, which the same answer's record carries.
DEFAULT_NON_CHARGEABLE_UNITS = ("requests", "sessions", "audioSeconds", "runtimeSeconds", "images")

#: Where a line's unit price came from, as its ``PriceOrigin`` says.
ORIGIN_SHIPPED = "shipped"
ORIGIN_CUSTOM = "custom"
ORIGIN_NONE = "none"

#: The values a source's ``basis`` may take, and how a line introduces each.
SOURCE_BASES = {
    "price-list-api": "AWS Price List API",
    "pricing-page": "AWS pricing page",
    "estimate": "Estimate",
}


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


def sources_for_region(pricing: Mapping[str, Any], region: str) -> Dict[str, Any]:
    """The source of each price in force for ``region``, by service.

    ``sources.default`` with ``sources.<region>`` merged over it: the overlay
    :func:`prices_for_region` applies to the prices. Read defensively, because a
    source is there to explain a number: an unreadable one costs the line its
    source and nothing else.
    """
    sources = pricing.get("sources")
    if not isinstance(sources, Mapping):
        return {}
    base = sources.get("default")
    own = sources.get(region)
    return deep_merge(
        base if isinstance(base, Mapping) else {},
        own if isinstance(own, Mapping) else {},
    )


def source_text(source: Any) -> Optional[str]:
    """One sentence naming where a shipped price came from, or ``None``."""
    if not isinstance(source, Mapping):
        return None
    basis = SOURCE_BASES.get(str(source.get("basis") or ""), "Source")
    parts = []
    if source.get("offer"):
        version = f" version {source['offerVersion']}" if source.get("offerVersion") else ""
        parts.append(f"offer {source['offer']}{version}")
    if source.get("usageType"):
        parts.append(f"usage type {source['usageType']}")
    if source.get("retrieved"):
        parts.append(f"read {source['retrieved']}")
    text = basis + (" (" + ", ".join(parts) + ")" if parts else "")
    if source.get("detail"):
        text = f"{text}. {source['detail']}"
    return text


def _price_entry(prices: Mapping[str, Any], service: str) -> Tuple[Any, Optional[str], str]:
    """The price entry of ``service``, a note when it is another id's, and its key."""
    entry = prices.get(service)
    if entry is not None:
        return entry, None, service
    for stand_in in price_stand_ins(service):
        if isinstance(prices.get(stand_in), Mapping):
            return (
                prices[stand_in],
                (
                    f"Priced at the listed price of {stand_in.partition('/')[2]}, the same model: "
                    "the price table does not list this id, whose own price may be higher."
                ),
                stand_in,
            )
    return None, None, service


def _unit_price(entry: Any, unit: str) -> Optional[float]:
    """The price of ``unit`` in a price entry, when it is a usable number."""
    if isinstance(entry, Mapping):
        value = entry.get(unit)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return None


def _money(value: float) -> str:
    """A unit price written into a sentence, in full and without an exponent."""
    return "$" + (f"{value:.12f}".rstrip("0").rstrip(".") or "0")


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


class _Tables:
    """The effective and the shipped tables of one region, read once per estimate."""

    def __init__(self, pricing: Mapping[str, Any], shipped: Mapping[str, Any]) -> None:
        self._pricing = pricing
        self._shipped = shipped
        self._cache: Dict[str, Tuple[Dict[str, Any], bool, Dict[str, Any], Dict[str, Any]]] = {}

    def region(self, region: str) -> Tuple[Dict[str, Any], bool, Dict[str, Any], Dict[str, Any]]:
        """Effective prices, whether they are the region's own, shipped prices, sources."""
        if region not in self._cache:
            prices, own = prices_for_region(self._pricing, region)
            shipped, _ = prices_for_region(self._shipped, region)
            self._cache[region] = (
                prices,
                own,
                shipped,
                sources_for_region(self._shipped, region),
            )
        return self._cache[region]


def _priced_line(
    line: Dict[str, Any],
    *,
    price: float,
    quantity: float,
    shipped_price: Optional[float],
    source: Any,
    profile: Optional[str],
) -> float:
    """Fill in a priced line: its cost, and where its price came from."""
    cost = quantity * price
    line.update(UnitPrice=price, Cost=round(cost, 8), Priced=True, ShippedUnitPrice=shipped_price)
    shipped_text = source_text(source)
    if shipped_price is not None and price == shipped_price:
        line["PriceOrigin"] = ORIGIN_SHIPPED
        line["PriceSource"] = shipped_text or "The price table LMA ships"
        line["PriceSourceUrl"] = source.get("url") if isinstance(source, Mapping) else None
        return cost
    where = f"the {profile} configuration profile" if profile else "this deployment's configuration"
    if shipped_price is None:
        text = f"Set in {where}. LMA ships no price for this unit."
    else:
        text = f"Set in {where}. The shipped price is {_money(shipped_price)}"
        text += f", from {shipped_text}." if shipped_text else "."
    line.update(PriceOrigin=ORIGIN_CUSTOM, PriceSource=text, PriceSourceUrl=None)
    return cost


def estimate_call_cost(
    call_id: str,
    records: Iterable[Mapping[str, Any]],
    pricing: Mapping[str, Any],
    deployment_region: str,
    *,
    shipped: Optional[Mapping[str, Any]] = None,
    profile: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """The itemised estimate for one meeting, in the ``CallCost`` GraphQL shape.

    Args:
        pricing: The price table in force: the baseline's with the active
            profile's merged over it.
        shipped: The baseline's table alone. A price that differs from it is
            reported as the profile's own, with the shipped price beside it.
            Defaults to ``pricing``, in which case every price is reported as
            shipped.
        profile: The name of the active configuration profile, for the line
            that reports a price it set.
    """
    records = list(records)
    shipped_table: Mapping[str, Any] = shipped if shipped is not None else pricing
    non_chargeable = set(pricing.get("nonChargeableUnits") or DEFAULT_NON_CHARGEABLE_UNITS)
    # Read defensively: a hand-repaired record could hold anything here, and an
    # unreadable note must cost the line its note rather than the whole estimate.
    notes_raw = pricing.get("notes")
    notes: Mapping[str, str] = notes_raw if isinstance(notes_raw, Mapping) else {}
    tables = _Tables(pricing, shipped_table)
    fallback_regions: set = set()

    items: List[Dict[str, Any]] = []
    context_totals: Dict[str, float] = {}
    total = 0.0
    unpriced = 0
    custom = 0

    grouped = aggregate(records, deployment_region)
    for context, service, region in sorted(
        grouped, key=lambda key: (_context_rank(key[0]), key[1], key[2])
    ):
        bucket = grouped[(context, service, region)]
        prices, own, shipped_prices, sources = tables.region(region)
        if not own:
            fallback_regions.add(region)
        entry, stand_in_note, priced_as = _price_entry(prices, service)
        shipped_entry = shipped_prices.get(priced_as)
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
                "Note": " ".join(n for n in (notes.get(service), stand_in_note) if n) or None,
                "Details": details or None,
                "PriceOrigin": None,
                "PriceSource": None,
                "PriceSourceUrl": None,
                "ShippedUnitPrice": None,
            }
            price = _unit_price(entry, unit)
            if unit in non_chargeable:
                line.update(Priced=True, UnitPrice=0.0, Cost=0.0)
            elif price is not None:
                cost = _priced_line(
                    line,
                    price=price,
                    quantity=quantity,
                    shipped_price=_unit_price(shipped_entry, unit),
                    source=sources.get(priced_as),
                    profile=profile,
                )
                if line["PriceOrigin"] == ORIGIN_CUSTOM:
                    custom += 1
                total += cost
                context_totals[context] = context_totals.get(context, 0.0) + cost
            elif quantity == 0:
                line.update(UnitPrice=0.0, Cost=0.0, Priced=True)
            else:
                unpriced += 1
                line.update(
                    PriceOrigin=ORIGIN_NONE,
                    PriceSource=(
                        f"The price table has no price for {unit} of {service} in {region}, so "
                        "this line is left out of the total."
                    ),
                )
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
        "CustomPriceCount": custom,
        "PriceProfile": profile,
        "RecordCount": len(records),
        "PricesAsOf": shipped_table.get("pricesAsOf"),
        "PriceSource": shipped_table.get("source"),
        "Region": deployment_region,
        "BaseRegion": pricing.get("baseRegion"),
        "RegionsWithoutListedPrices": sorted(fallback_regions),
        "NotIncluded": list(pricing.get("notIncluded") or []),
        "ComputedAt": moment.astimezone(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
    }
