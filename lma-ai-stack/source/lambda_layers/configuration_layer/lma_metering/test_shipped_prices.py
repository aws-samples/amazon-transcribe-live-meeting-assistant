#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""What the shipped price table guarantees.

The table is ``configuration_seeder/defaults/CallPricing.json``, which the
seeder writes into the baseline at deploy time. Asserted here: every unit LMA
meters in the regions it deploys to has a price under its exact key, every price
says where it came from, and the table is one its own schema accepts.

"Exact key" is not "read for that region": a plain Amazon Nova id configured in
eu-west-1, where Amazon Bedrock offers no in-region Nova, resolves to the base
region's price. Such an id cannot be called there, so no estimate meets it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple

import pytest
from lma_config import inference_geography as geo
from lma_config import schema

from lma_metering import pricing

SHIPPED_FILE = (
    Path(__file__).resolve().parents[3]
    / "lambda_functions"
    / "configuration_seeder"
    / "defaults"
    / "CallPricing.json"
)
SHIPPED: Dict[str, Any] = json.loads(SHIPPED_FILE.read_text(encoding="utf-8"))

#: The regions LMA's stack can be launched in (docs/prerequisites-and-deployment.md).
LAUNCH_REGIONS = ("us-east-1", "us-west-2", "eu-west-1", "ap-northeast-1")
GOVCLOUD = "us-gov-west-1"

#: Every non-Bedrock service a writer meters, with its chargeable units.
FIXED_SERVICES: Dict[str, Tuple[str, ...]] = {
    "transcribe/streaming": ("seconds",),
    "transcribe/streaming-pii-redaction": ("seconds",),
    "transcribe/streaming-custom-language-model": ("seconds",),
    "transcribe/batch": ("seconds",),
    "fargate/x86_64": ("vcpuSeconds", "gbSeconds"),
    "fargate/arm64": ("vcpuSeconds", "gbSeconds"),
    "lambda-microvm/arm64": ("vcpuSeconds", "gbSeconds"),
    "lambda-microvm/x86_64": ("vcpuSeconds", "gbSeconds"),
    "comprehend/detect-sentiment": ("units",),
}

#: The token units a Bedrock text model is billed in. A Claude model is billed
#: for cache writes; Amazon Nova lists them at zero, and Nova Premier lists no
#: cache-write price at all.
CLAUDE_UNITS = ("inputTokens", "outputTokens", "cacheReadInputTokens", "cacheWriteInputTokens")
NOVA_UNITS = ("inputTokens", "outputTokens", "cacheReadInputTokens")

MODEL_SETTINGS = (
    ("language_models", "summaryModelId"),
    ("language_models", "meetingAssistModelId"),
    ("language_models", "knowledgeBaseModelId"),
    ("language_models", "keyframeTextModelId"),
    ("meeting_consent", "verifierModelId"),
)

DATE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")


def offered_models() -> List[str]:
    ids: Set[str] = set()
    for section, setting in MODEL_SETTINGS:
        ids.update(schema.section_schema(section)["properties"][setting]["enum"])
    return sorted(ids)


def metered_model_ids(region: str) -> Set[str]:
    """Every Bedrock id a deployment in ``region`` can call, under any geography."""
    ids: Set[str] = set()
    for geography in geo.GEOGRAPHIES:
        for model_id in offered_models():
            try:
                ids.add(geo.resolve_for_geography(model_id, geography, region).model_id)
            except geo.ModelUnavailableInGeography:
                continue
    return ids


def token_units(model_id: str) -> Tuple[str, ...]:
    return CLAUDE_UNITS if "anthropic." in model_id else NOVA_UNITS


@pytest.mark.parametrize("region", LAUNCH_REGIONS)
def test_every_metered_unit_has_a_shipped_price(region: str) -> None:
    """Exactly, under its own key: no line in these regions needs a stand-in price."""
    prices, own = pricing.prices_for_region(SHIPPED, region)
    assert own, f"{region} is not listed as a region with prices of its own"
    missing = [
        f"{service} {unit}"
        for service, units in FIXED_SERVICES.items()
        for unit in units
        if not isinstance((prices.get(service) or {}).get(unit), (int, float))
    ]
    for model_id in sorted(metered_model_ids(region)):
        entry = prices.get(f"bedrock/{model_id}") or {}
        missing += [f"{model_id} {u}" for u in token_units(model_id) if u not in entry]
    assert not missing, f"{region}: " + ", ".join(missing)


@pytest.mark.parametrize("region", ("us-east-1", "us-west-2", "ap-northeast-1"))
def test_the_voice_assistant_is_priced_where_nova_sonic_runs(region: str) -> None:
    prices, _ = pricing.prices_for_region(SHIPPED, region)
    entry = prices["bedrock/amazon.nova-2-sonic-v1:0"]
    for unit in ("speechInputTokens", "speechOutputTokens", "textInputTokens", "textOutputTokens"):
        assert unit in entry, unit


def test_govcloud_prices_what_it_offers_from_its_own_price_list() -> None:
    prices, own = pricing.prices_for_region(SHIPPED, GOVCLOUD)
    sources = pricing.sources_for_region(SHIPPED, GOVCLOUD)
    assert own
    for service in (
        "transcribe/streaming",
        "transcribe/batch",
        "fargate/x86_64",
        "fargate/arm64",
        "comprehend/detect-sentiment",
        "bedrock/amazon.nova-lite-v1:0",
        "bedrock/us-gov.anthropic.claude-sonnet-4-5-20250929-v1:0",
    ):
        assert service in prices, service
        assert sources[service]["usageType"].startswith("UGW1-"), service


def test_every_shipped_price_says_where_it_came_from() -> None:
    for region, services in SHIPPED["regions"].items():
        sources = pricing.sources_for_region(SHIPPED, region)
        for service in services:
            source = sources.get(service)
            assert isinstance(source, dict), f"{region} {service} has no source"
            assert source["basis"] in pricing.SOURCE_BASES, f"{region} {service}"
            assert DATE.match(source["retrieved"]), f"{region} {service}"
            assert source["url"].startswith("https://aws.amazon.com/"), f"{region} {service}"
            if source["basis"] == "price-list-api":
                assert source["offer"] and source["offerVersion"] and source["usageType"]
            else:
                assert source.get("detail"), f"{region} {service}: say why it is not read"


def test_a_regions_own_price_is_documented_by_that_regions_own_source() -> None:
    for region, services in SHIPPED["regions"].items():
        if region == "default":
            continue
        for service in services:
            assert service in SHIPPED["sources"].get(region, {}), f"{region} {service}"


def test_every_source_documents_a_price_the_table_has() -> None:
    for region, services in SHIPPED["sources"].items():
        prices, _ = pricing.prices_for_region(SHIPPED, region)
        for service in services:
            assert service in prices, f"{region} {service} has a source and no price"


def test_the_shipped_prices_are_dated_when_they_were_read() -> None:
    dates = {
        source["retrieved"]
        for services in SHIPPED["sources"].values()
        for source in services.values()
    }
    assert dates == {SHIPPED["pricesAsOf"]}
    assert set(SHIPPED["pricedRegions"]) == set(LAUNCH_REGIONS) | {GOVCLOUD}


def test_the_shipped_table_is_one_its_own_schema_accepts() -> None:
    schema.validate_section("call_pricing", SHIPPED)


def test_the_shipped_text_reads_as_a_description_of_the_estimate() -> None:
    """The notes and lists are shown to people on every estimate; none is an instruction."""
    texts = [*SHIPPED["notes"].values(), *SHIPPED["notIncluded"], SHIPPED["source"]]
    for text in texts:
        assert not re.search(r"\b(you must|always|never|do not|ignore)\b", text, re.IGNORECASE), (
            text
        )
