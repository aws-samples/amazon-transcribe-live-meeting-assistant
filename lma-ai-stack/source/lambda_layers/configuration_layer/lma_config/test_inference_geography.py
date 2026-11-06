#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""Tests for the Model inference geography resolver (no AWS)."""

from __future__ import annotations

import logging

import pytest

from lma_config import inference_geography as geo
from lma_config import schema
from lma_config.inference_geography import (
    GEOGRAPHY,
    GLOBAL,
    IN_REGION,
    ModelUnavailableInGeography,
    resolve_for_geography,
)

SONNET = "global.anthropic.claude-sonnet-5-5"
HAIKU_45 = "global.anthropic.claude-haiku-4-5-20251001-v1:0"
NOVA_LITE = "amazon.nova-lite-v1:0"


def resolved(model_id: str, geography: str, region: str) -> str:
    return resolve_for_geography(model_id, geography, region).model_id


# ── parsing ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "model_id, prefix, base",
    [
        (SONNET, "global", "anthropic.claude-sonnet-5-5"),
        (
            "us-gov.anthropic.claude-sonnet-4-5-20250929-v1:0",
            "us-gov",
            "anthropic.claude-sonnet-4-5-20250929-v1:0",
        ),
        ("jp.amazon.nova-2-lite-v1:0", "jp", "amazon.nova-2-lite-v1:0"),
        (NOVA_LITE, None, NOVA_LITE),
        ("anthropic.claude-haiku-5-5", None, "anthropic.claude-haiku-5-5"),
        ("", None, ""),
    ],
)
def test_split_model_id(model_id, prefix, base) -> None:
    assert geo.split_model_id(model_id) == (prefix, base)


def test_model_arn_addresses_profiles_and_models() -> None:
    assert geo.model_arn("eu.anthropic.claude-sonnet-5-5", "eu-west-1", "123456789012") == (
        "arn:aws:bedrock:eu-west-1:123456789012:inference-profile/eu.anthropic.claude-sonnet-5-5"
    )
    assert geo.model_arn(NOVA_LITE, "us-east-1", "123456789012") == (
        "arn:aws:bedrock:us-east-1::foundation-model/amazon.nova-lite-v1:0"
    )


@pytest.mark.parametrize(
    "region, prefixes",
    [
        ("us-east-1", ("us",)),
        ("us-west-2", ("us",)),
        ("us-gov-west-1", ("us-gov",)),
        ("ca-central-1", ("ca",)),
        ("eu-west-1", ("eu",)),
        ("eu-west-2", ("eu",)),
        ("ap-northeast-1", ("jp", "apac")),
        ("ap-southeast-2", ("au", "apac")),
        ("ap-south-1", ("in", "apac")),
        ("ap-southeast-1", ("apac",)),
        ("ap-northeast-2", ("apac",)),
        ("sa-east-1", ()),
        ("me-central-1", ()),
    ],
)
def test_geography_of_each_region(region, prefixes) -> None:
    assert geo.geography_prefixes(region) == prefixes


# ── Global: unchanged ───────────────────────────────────────────────────────


@pytest.mark.parametrize("region", ["us-east-1", "eu-west-1", "sa-east-1", "nowhere-1"])
@pytest.mark.parametrize("model_id", [SONNET, NOVA_LITE, "us.anthropic.claude-haiku-5-5"])
def test_global_leaves_every_id_as_chosen(model_id, region) -> None:
    assert resolved(model_id, GLOBAL, region) == model_id


def test_an_unknown_geography_value_is_treated_as_global() -> None:
    assert resolved(SONNET, "somewhere", "eu-west-1") == SONNET


# ── Same geography ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "region, model_id, expected",
    [
        # The shipped defaults, per geography.
        ("us-east-1", SONNET, "us.anthropic.claude-sonnet-5-5"),
        ("us-west-2", HAIKU_45, "us.anthropic.claude-haiku-4-5-20251001-v1:0"),
        ("eu-west-1", SONNET, "eu.anthropic.claude-sonnet-5-5"),
        ("eu-central-1", HAIKU_45, "eu.anthropic.claude-haiku-4-5-20251001-v1:0"),
        ("ap-southeast-2", SONNET, "au.anthropic.claude-sonnet-5-5"),
        ("ap-northeast-1", HAIKU_45, "jp.anthropic.claude-haiku-4-5-20251001-v1:0"),
        ("ap-south-1", HAIKU_45, "in.anthropic.claude-haiku-4-5-20251001-v1:0"),
        # A country profile first, then the region's broader geography.
        ("ap-northeast-1", "global.amazon.nova-2-lite-v1:0", "jp.amazon.nova-2-lite-v1:0"),
        ("ap-northeast-1", "us.amazon.nova-pro-v1:0", "apac.amazon.nova-pro-v1:0"),
        # Another geography's profile is moved into this one.
        ("eu-west-1", "us.anthropic.claude-haiku-5-5", "eu.anthropic.claude-haiku-5-5"),
        # Canada's geography is Canada, not the United States.
        ("ca-central-1", "global.anthropic.claude-haiku-5-5", "ca.anthropic.claude-haiku-5-5"),
    ],
)
def test_same_geography_maps_to_the_geography_profile(region, model_id, expected) -> None:
    resolution = resolve_for_geography(model_id, GEOGRAPHY, region)
    assert resolution.model_id == expected
    assert resolution.verified


@pytest.mark.parametrize(
    "region, model_id",
    [
        ("us-east-1", "us.anthropic.claude-sonnet-5-5"),
        ("us-east-1", NOVA_LITE),
        ("ap-northeast-1", "apac.amazon.nova-pro-v1:0"),
    ],
)
def test_an_id_already_within_the_geography_is_kept(region, model_id) -> None:
    assert resolved(model_id, GEOGRAPHY, region) == model_id


def test_a_plain_id_the_region_does_not_offer_uses_the_geography_profile() -> None:
    # Amazon Bedrock does not offer Nova Lite on demand in eu-west-1; its EU
    # profile is offered there.
    assert resolved(NOVA_LITE, GEOGRAPHY, "eu-west-1") == "eu.amazon.nova-lite-v1:0"


@pytest.mark.parametrize(
    "region, model_id",
    [
        # No Brazilian profile, and Claude is not offered in-region there.
        ("sa-east-1", SONNET),
        # Canada has a profile for Claude Haiku 5.5 only.
        ("ca-central-1", SONNET),
        # No APAC or Korean profile for Claude.
        ("ap-northeast-2", SONNET),
    ],
)
def test_no_id_in_the_geography_is_refused_rather_than_left_global(region, model_id) -> None:
    with pytest.raises(ModelUnavailableInGeography, match="Model inference geography"):
        resolve_for_geography(model_id, GEOGRAPHY, region)


def test_a_region_the_catalogue_lacks_is_mapped_by_rule_unchecked() -> None:
    resolution = resolve_for_geography(SONNET, GEOGRAPHY, "us-gov-west-1")
    assert resolution.model_id == "us-gov.anthropic.claude-sonnet-5-5"
    assert not resolution.verified
    # A region with no geography profile gets the plain id, never the global one.
    assert resolved(SONNET, GEOGRAPHY, "me-central-1") == "anthropic.claude-sonnet-5-5"


# ── In-region only ──────────────────────────────────────────────────────────


def test_in_region_uses_the_plain_id_where_offered() -> None:
    assert resolved("us.amazon.nova-lite-v1:0", IN_REGION, "us-east-1") == NOVA_LITE
    assert resolved("global.anthropic.claude-opus-5-5", IN_REGION, "eu-west-2") == (
        "anthropic.claude-opus-5-5"
    )
    assert resolved(NOVA_LITE, IN_REGION, "us-east-1") == NOVA_LITE


def test_in_region_refuses_a_model_the_region_does_not_offer() -> None:
    # Claude Sonnet 5.5 is offered in us-east-1 through inference profiles only.
    with pytest.raises(ModelUnavailableInGeography, match="in us-east-1"):
        resolve_for_geography(SONNET, IN_REGION, "us-east-1")


# ── the catalogue and the schema ────────────────────────────────────────────


def test_every_offered_model_is_in_the_catalogue() -> None:
    catalogued = set(geo.catalog()["models"])
    for section, setting in (
        ("language_models", "summaryModelId"),
        ("language_models", "meetingAssistModelId"),
        ("language_models", "knowledgeBaseModelId"),
        ("language_models", "keyframeTextModelId"),
        ("meeting_consent", "verifierModelId"),
    ):
        offered = schema.section_schema(section)["properties"][setting]["enum"]
        missing = {geo.base_model_id(m) for m in offered} - catalogued
        assert not missing, f"{section}.{setting}: run scripts/bedrock_inference_catalog.py"


def test_the_catalogue_names_only_known_prefixes() -> None:
    known = set(geo.profile_prefixes())
    for entry in geo.catalog()["geographies"]:
        assert set(entry["prefixes"]) <= known
    for listed in geo.catalog()["regions"].values():
        for profile in listed["profiles"]:
            assert geo.split_model_id(profile)[0] in known - {"global"}


def test_the_setting_is_in_the_schema_and_defaults_to_global() -> None:
    declared = schema.section_schema("language_models")["properties"]["inferenceGeography"]
    assert declared["enum"] == list(geo.GEOGRAPHIES)
    assert set(declared["enumLabels"]) == set(geo.GEOGRAPHIES)
    schema.validate_section("language_models", {"inferenceGeography": "geography"})
    with pytest.raises(Exception):
        schema.validate_section("language_models", {"inferenceGeography": "eu"})


# ── resolve_model_id: the reader's entry point ──────────────────────────────


@pytest.fixture(autouse=True)
def _fresh_log_memory():
    geo._LOGGED.clear()
    yield
    geo._LOGGED.clear()


def _with_geography(monkeypatch, value) -> None:
    monkeypatch.setattr(geo, "configured_geography", lambda store=None: value)


def test_resolve_model_id_reads_the_setting_and_the_region(monkeypatch) -> None:
    _with_geography(monkeypatch, GEOGRAPHY)
    monkeypatch.setenv("AWS_REGION", "eu-west-1")
    assert geo.resolve_model_id(SONNET) == "eu.anthropic.claude-sonnet-5-5"
    assert geo.resolve_model_id(SONNET, region="us-east-1") == "us.anthropic.claude-sonnet-5-5"


def test_resolve_model_id_under_global_returns_the_id_untouched(monkeypatch) -> None:
    _with_geography(monkeypatch, GLOBAL)
    assert geo.resolve_model_id(SONNET, region="eu-west-1") == SONNET


def test_resolve_model_id_logs_a_change_once(monkeypatch, caplog) -> None:
    _with_geography(monkeypatch, GEOGRAPHY)
    with caplog.at_level(logging.INFO, logger=geo.LOGGER.name):
        geo.resolve_model_id(SONNET, region="us-east-1")
        geo.resolve_model_id(SONNET, region="us-east-1")
    assert len([r for r in caplog.records if "is run as" in r.getMessage()]) == 1


def test_configured_geography_defaults_to_global_without_a_table(monkeypatch) -> None:
    monkeypatch.delenv("CONFIGURATION_TABLE_NAME", raising=False)
    assert geo.configured_geography() == GLOBAL


# ── pricing stand-ins ───────────────────────────────────────────────────────


def test_price_stand_ins_prefer_the_global_price() -> None:
    assert geo.price_stand_ins("bedrock/eu.anthropic.claude-sonnet-5-5") == [
        "bedrock/global.anthropic.claude-sonnet-5-5",
        "bedrock/anthropic.claude-sonnet-5-5",
        "bedrock/us.anthropic.claude-sonnet-5-5",
    ]
    assert geo.price_stand_ins("bedrock/amazon.nova-lite-v1:0") == [
        "bedrock/global.amazon.nova-lite-v1:0",
        "bedrock/us.amazon.nova-lite-v1:0",
    ]
    assert geo.price_stand_ins("transcribe/streaming") == []
