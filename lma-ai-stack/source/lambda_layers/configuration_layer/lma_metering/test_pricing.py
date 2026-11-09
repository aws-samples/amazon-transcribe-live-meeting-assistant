#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Tests for turning metering records into a cost estimate.

The lookup is strict on purpose -- an exact service and an exact unit, or no
price at all -- and an unpriced line is excluded from every total and counted,
so a reader of the estimate can tell the total is incomplete instead of being shown a confident
wrong number.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

import pytest

from lma_metering import pricing

NOW = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)

TABLE: Dict[str, Any] = {
    "currency": "USD",
    "pricesAsOf": "2026-10-28",
    "source": "test",
    "baseRegion": "us-east-1",
    "pricedRegions": ["us-east-1", "us-west-2"],
    "regions": {
        "default": {
            "bedrock/m": {"inputTokens": 0.000001, "outputTokens": 0.000005},
            "transcribe/streaming": {"seconds": 0.0001667},
        },
        "us-west-2": {"bedrock/m": {"outputTokens": 0.000006}},
    },
    "nonChargeableUnits": ["requests"],
    "notes": {"transcribe/streaming": "a note"},
    "notIncluded": ["NAT gateways"],
}


def record(context: str, service: str, usage: Dict[str, float], **extra: Any) -> Dict[str, Any]:
    return {"Context": context, "Service": service, "Usage": usage, **extra}


def test_a_region_entry_overrides_only_what_it_names() -> None:
    prices, own = pricing.prices_for_region(TABLE, "us-west-2")
    assert own
    assert prices["bedrock/m"] == {"inputTokens": 0.000001, "outputTokens": 0.000006}
    assert prices["transcribe/streaming"] == {"seconds": 0.0001667}


def test_a_region_the_table_does_not_list_uses_the_base_and_says_so() -> None:
    prices, own = pricing.prices_for_region(TABLE, "eu-west-1")
    assert own is False
    assert prices["bedrock/m"]["outputTokens"] == 0.000005


def test_usage_is_summed_by_context_and_service() -> None:
    totals = pricing.aggregate(
        [
            record("Summary", "bedrock/m", {"inputTokens": 100}),
            record("Summary", "bedrock/m", {"inputTokens": 50, "outputTokens": 10}),
            record("SpeakerNaming", "bedrock/m", {"inputTokens": 7}),
        ],
        "us-east-1",
    )
    assert totals[("Summary", "bedrock/m", "us-east-1")]["usage"] == {
        "inputTokens": 150.0,
        "outputTokens": 10.0,
    }
    assert totals[("Summary", "bedrock/m", "us-east-1")]["records"] == 2
    assert totals[("SpeakerNaming", "bedrock/m", "us-east-1")]["usage"] == {"inputTokens": 7.0}


GEO_TABLE: Dict[str, Any] = {
    **TABLE,
    "regions": {
        "default": {
            "bedrock/global.anthropic.claude-sonnet-5-5": {
                "inputTokens": 0.000002,
                "outputTokens": 0.00001,
            },
        }
    },
}


def test_a_geography_profile_is_priced_as_the_same_models_global_profile_and_says_so() -> None:
    result = pricing.estimate_call_cost(
        "c1",
        [record("Summary", "bedrock/eu.anthropic.claude-sonnet-5-5", {"inputTokens": 1000})],
        GEO_TABLE,
        "eu-west-1",
        now=NOW,
    )
    (line,) = result["Items"]
    assert line["Priced"] and line["UnitPrice"] == 0.000002
    assert "global.anthropic.claude-sonnet-5-5" in line["Note"]
    assert result["UnpricedCount"] == 0


def test_a_different_model_is_never_a_stand_in() -> None:
    result = pricing.estimate_call_cost(
        "c1",
        [record("Summary", "bedrock/eu.anthropic.claude-haiku-5-5", {"inputTokens": 1000})],
        GEO_TABLE,
        "eu-west-1",
        now=NOW,
    )
    assert result["UnpricedCount"] == 1
    assert result["Items"][0]["Note"] is None


def test_the_estimate_multiplies_each_unit_by_its_price() -> None:
    result = pricing.estimate_call_cost(
        "c",
        [
            record(
                "Summary", "bedrock/m", {"inputTokens": 1000, "outputTokens": 200, "requests": 1}
            ),
            record("Transcription", "transcribe/streaming", {"seconds": 600}),
        ],
        TABLE,
        "us-east-1",
        now=NOW,
    )
    assert result["TotalCost"] == pytest.approx(0.001 + 0.001 + 0.10002)
    assert result["UnpricedCount"] == 0
    assert [t["Context"] for t in result["ContextTotals"]] == ["Transcription", "Summary"]
    requests = next(i for i in result["Items"] if i["Unit"] == "requests")
    assert requests["Chargeable"] is False and requests["Cost"] == 0.0
    transcription = next(i for i in result["Items"] if i["Context"] == "Transcription")
    assert transcription["Note"] == "a note"
    assert result["PricesAsOf"] == "2026-10-28"
    assert result["NotIncluded"] == ["NAT gateways"]
    assert result["ComputedAt"] == "2026-10-28T12:00:00Z"


def test_an_unknown_service_is_unpriced_and_left_out_of_the_total() -> None:
    result = pricing.estimate_call_cost(
        "c",
        [
            record("Summary", "bedrock/m", {"inputTokens": 1000}),
            record("Summary", "bedrock/m-similar", {"inputTokens": 1000}),
        ],
        TABLE,
        "us-east-1",
        now=NOW,
    )
    assert result["TotalCost"] == pytest.approx(0.001)
    assert result["UnpricedCount"] == 1
    unpriced = next(i for i in result["Items"] if i["Service"] == "bedrock/m-similar")
    assert unpriced["Priced"] is False and unpriced["Cost"] is None


def test_a_known_service_with_an_unknown_unit_is_unpriced() -> None:
    result = pricing.estimate_call_cost(
        "c", [record("Summary", "bedrock/m", {"cacheReadInputTokens": 10})], TABLE, "us-east-1"
    )
    assert result["UnpricedCount"] == 1
    assert result["TotalCost"] == 0


def test_a_record_billed_in_another_region_is_priced_there() -> None:
    result = pricing.estimate_call_cost(
        "c",
        [record("VoiceAssistant", "bedrock/m", {"outputTokens": 1000}, Region="us-west-2")],
        TABLE,
        "us-east-1",
    )
    assert result["TotalCost"] == pytest.approx(0.006)
    assert result["Items"][0]["Region"] == "us-west-2"


def test_a_region_without_prices_is_reported() -> None:
    result = pricing.estimate_call_cost(
        "c", [record("Summary", "bedrock/m", {"inputTokens": 1})], TABLE, "ap-south-1"
    )
    assert result["RegionsWithoutListedPrices"] == ["ap-south-1"]


def test_a_meeting_with_no_records_costs_nothing_and_says_how_many_it_read() -> None:
    result = pricing.estimate_call_cost("c", [], TABLE, "us-east-1")
    assert result["TotalCost"] == 0 and result["Items"] == [] and result["RecordCount"] == 0


def test_differing_details_are_kept_side_by_side() -> None:
    totals = pricing.aggregate(
        [
            record("VirtualParticipant", "fargate/x86_64", {"vcpuSeconds": 1}, Details={"a": "x"}),
            record("VirtualParticipant", "fargate/x86_64", {"vcpuSeconds": 1}, Details={"a": "y"}),
        ],
        "us-east-1",
    )
    assert totals[("VirtualParticipant", "fargate/x86_64", "us-east-1")]["details"] == {"a": "x, y"}


def test_the_shipped_table_prices_every_service_a_writer_meters() -> None:
    """Every Service string a writer in this repository uses has a price, or a note
    saying why it deliberately has none."""
    shipped = json.loads(
        (
            Path(__file__).resolve().parents[3]
            / "lambda_functions"
            / "configuration_seeder"
            / "defaults"
            / "CallPricing.json"
        ).read_text(encoding="utf-8")
    )
    base = shipped["regions"]["default"]
    fixed_services = [
        "transcribe/streaming",
        "transcribe/streaming-pii-redaction",
        "transcribe/streaming-custom-language-model",
        "lambda-microvm/arm64",
        "fargate/x86_64",
        "fargate/arm64",
        "comprehend/detect-sentiment",
        "bedrock/amazon.nova-2-sonic-v1:0",
    ]
    for service in fixed_services:
        assert service in base, service
    assert "ec2/virtual-participant-host" in shipped["notes"]


# ── where each line's price came from ───────────────────────────────────────

SOURCED: Dict[str, Any] = {
    **TABLE,
    "sources": {
        "default": {
            "transcribe/streaming": {
                "basis": "price-list-api",
                "offer": "transcribe",
                "offerVersion": "20260911124646",
                "usageType": "USE1-StreamingAudio",
                "retrieved": "2026-11-09",
                "url": "https://aws.amazon.com/transcribe/pricing/",
            },
            "bedrock/m": {"basis": "estimate", "detail": "not listed", "retrieved": "2026-11-09"},
        },
        "us-west-2": {
            "bedrock/m": {
                "basis": "price-list-api",
                "offer": "AmazonBedrock",
                "usageType": "USW2-m",
                "retrieved": "2026-11-09",
            }
        },
    },
}


def _with_default_price(table: Dict[str, Any], service: str, units: Dict[str, float]) -> Dict:
    """``table`` with ``service``'s base-region prices changed, as a profile changes them."""
    default = table["regions"]["default"]
    return {
        **table,
        "regions": {
            **table["regions"],
            "default": {**default, service: {**default.get(service, {}), **units}},
        },
    }


def test_the_meeting_cost_shows_units_unit_price_and_cost_per_line_with_its_source() -> None:
    result = pricing.estimate_call_cost(
        "c",
        [record("Transcription", "transcribe/streaming", {"seconds": 600})],
        SOURCED,
        "us-east-1",
        now=NOW,
    )
    (line,) = result["Items"]
    assert line["Quantity"] == 600
    assert line["UnitPrice"] == 0.0001667
    assert line["Cost"] == pytest.approx(600 * 0.0001667)
    assert line["PriceOrigin"] == pricing.ORIGIN_SHIPPED
    assert line["ShippedUnitPrice"] == 0.0001667
    assert line["PriceSource"] == (
        "AWS Price List API (offer transcribe version 20260911124646, "
        "usage type USE1-StreamingAudio, read 2026-11-09)"
    )
    assert line["PriceSourceUrl"] == "https://aws.amazon.com/transcribe/pricing/"
    assert result["CustomPriceCount"] == 0


def test_an_override_price_replaces_the_shipped_price_in_the_estimate() -> None:
    overridden = _with_default_price(SOURCED, "transcribe/streaming", {"seconds": 0.0001})
    result = pricing.estimate_call_cost(
        "c",
        [record("Transcription", "transcribe/streaming", {"seconds": 600})],
        overridden,
        "us-east-1",
        shipped=SOURCED,
        profile="negotiated",
        now=NOW,
    )
    (line,) = result["Items"]
    assert line["UnitPrice"] == 0.0001
    assert result["TotalCost"] == pytest.approx(0.06)
    assert line["PriceOrigin"] == pricing.ORIGIN_CUSTOM
    assert line["ShippedUnitPrice"] == 0.0001667
    assert line["PriceSource"].startswith("Set in the negotiated configuration profile.")
    assert "$0.0001667" in line["PriceSource"]
    assert "AWS Price List API" in line["PriceSource"]
    assert line["PriceSourceUrl"] is None
    assert result["CustomPriceCount"] == 1
    assert result["PriceProfile"] == "negotiated"


def test_a_price_for_a_unit_the_release_does_not_price_is_reported_as_the_profiles() -> None:
    added = _with_default_price(SOURCED, "bedrock/m", {"imagesIn": 0.01})
    result = pricing.estimate_call_cost(
        "c",
        [record("Summary", "bedrock/m", {"imagesIn": 2})],
        added,
        "us-east-1",
        shipped=SOURCED,
        profile="custom",
    )
    (line,) = result["Items"]
    assert line["PriceOrigin"] == pricing.ORIGIN_CUSTOM
    assert line["ShippedUnitPrice"] is None
    assert "LMA ships no price for this unit" in line["PriceSource"]


def test_a_region_names_the_source_of_its_own_prices() -> None:
    result = pricing.estimate_call_cost(
        "c",
        [record("VoiceAssistant", "bedrock/m", {"outputTokens": 10}, Region="us-west-2")],
        SOURCED,
        "us-east-1",
    )
    (line,) = result["Items"]
    assert line["UnitPrice"] == 0.000006
    assert "usage type USW2-m" in line["PriceSource"]


def test_an_estimated_shipped_price_says_it_is_an_estimate() -> None:
    result = pricing.estimate_call_cost(
        "c", [record("Summary", "bedrock/m", {"inputTokens": 10})], SOURCED, "us-east-1"
    )
    assert result["Items"][0]["PriceSource"] == "Estimate (read 2026-11-09). not listed"


def test_an_unpriced_line_says_it_is_left_out_of_the_total() -> None:
    result = pricing.estimate_call_cost(
        "c", [record("Summary", "bedrock/other", {"inputTokens": 10})], SOURCED, "us-east-1"
    )
    (line,) = result["Items"]
    assert line["PriceOrigin"] == pricing.ORIGIN_NONE
    assert "left out of the total" in line["PriceSource"]


def test_a_stand_in_price_is_sourced_from_the_entry_it_was_taken_from() -> None:
    table = {
        **GEO_TABLE,
        "sources": {
            "default": {
                "bedrock/global.anthropic.claude-sonnet-5-5": {
                    "basis": "price-list-api",
                    "usageType": "global-sku",
                }
            }
        },
    }
    result = pricing.estimate_call_cost(
        "c",
        [record("Summary", "bedrock/eu.anthropic.claude-sonnet-5-5", {"inputTokens": 1000})],
        table,
        "eu-west-1",
    )
    (line,) = result["Items"]
    assert line["PriceOrigin"] == pricing.ORIGIN_SHIPPED
    assert "global-sku" in line["PriceSource"]


def test_an_unreadable_source_costs_the_line_its_source_and_nothing_else() -> None:
    broken = {**SOURCED, "sources": {"default": "nonsense"}}
    result = pricing.estimate_call_cost(
        "c", [record("Transcription", "transcribe/streaming", {"seconds": 60})], broken, "us-east-1"
    )
    (line,) = result["Items"]
    assert line["Priced"] and line["PriceSource"] == "The price table LMA ships"
