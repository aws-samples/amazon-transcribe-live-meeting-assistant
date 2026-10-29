#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Tests for the section registry and the cache tiers it declares.

The point of the registry is that a section's caching policy is decided once, in
its declaration, and cannot be chosen or overridden where it is read. Two tests
here are about that rather than about any value:

* ``test_no_read_api_accepts_a_caching_argument`` reads the accessor's own
  signatures. LMA's existing readers each picked their own policy at the call
  site, and the four that resulted disagree; an argument that let a call site do
  that again would put the design straight back where it started.
* ``test_an_unregistered_section_is_refused`` pins that the alternative to a
  declaration is an error, not a default.

The tier TTLs are asserted as exact numbers because each one preserves an
existing reader's measured policy: 30 seconds is the transcriber's ASR cache and
300 seconds is the MCP server loader's.
"""

from __future__ import annotations

import inspect

import pytest

from lma_config import sections, store
from lma_config.errors import UnknownConfigurationSection


def test_there_are_exactly_three_tiers() -> None:
    assert [tier.value for tier in sections.CacheTier] == ["immediate", "short", "standard"]


def test_each_tier_declares_the_ttl_the_existing_readers_use() -> None:
    """One number per tier, and the immediate tier's is no cache at all."""
    assert sections.TIER_TTL_SECONDS[sections.CacheTier.IMMEDIATE] == 0.0
    assert sections.TIER_TTL_SECONDS[sections.CacheTier.SHORT] == 30.0
    assert sections.TIER_TTL_SECONDS[sections.CacheTier.STANDARD] == 300.0


def test_only_the_immediate_tier_reads_consistently() -> None:
    """A value promised to apply at once cannot be read from a stale replica.

    The other two tiers tolerate a TTL measured in tens of seconds, which is
    orders of magnitude more than DynamoDB's replication lag, so a consistent
    read would cost twice the capacity and change nothing.
    """
    assert sections.TIER_CONSISTENT_READ == {
        sections.CacheTier.IMMEDIATE: True,
        sections.CacheTier.SHORT: False,
        sections.CacheTier.STANDARD: False,
    }


def test_every_tier_has_both_a_ttl_and_a_read_policy() -> None:
    """A tier added later must declare both, or reads of it would be undefined."""
    for tier in sections.CacheTier:
        assert tier in sections.TIER_TTL_SECONDS
        assert tier in sections.TIER_CONSISTENT_READ


def test_an_unregistered_section_is_refused() -> None:
    """The alternative to a declared policy is an error, not a guess."""
    with pytest.raises(UnknownConfigurationSection) as raised:
        sections.section_spec("not_registered_anywhere")
    assert "summary_prompt_templates" in str(raised.value), (
        "the message should list what is registered, so the fix is obvious"
    )


def test_every_registered_section_declares_a_tier_and_a_description() -> None:
    registry = sections.registered_sections()
    assert registry, "the registry is empty, so the rest of this file checks nothing"
    for name, spec in registry.items():
        assert spec.name == name
        assert isinstance(spec.cache_tier, sections.CacheTier)
        assert spec.description.strip(), f"{name} has no description"


def test_the_summary_prompt_templates_section_is_on_the_standard_tier() -> None:
    """Prompts are the standard tier's reason for existing.

    The page that edits them does not promise an immediate effect, and the
    reader is a summary that runs minutes after a meeting ends.
    """
    spec = sections.section_spec("summary_prompt_templates")
    assert spec is sections.SUMMARY_PROMPT_TEMPLATES
    assert spec.cache_tier is sections.CacheTier.STANDARD


def test_the_nova_sonic_section_is_on_the_immediate_tier() -> None:
    """The page promises the next meeting uses what was saved.

    Its reader runs once per meeting, when the voice assistant starts, and read
    the previous table with no cache at all. Any TTL would add a window in which
    a meeting started after a save did not get it.
    """
    spec = sections.section_spec("nova_sonic_config")
    assert spec is sections.NOVA_SONIC_CONFIG
    assert spec.cache_tier is sections.CacheTier.IMMEDIATE


def test_the_chat_buttons_section_is_on_the_immediate_tier() -> None:
    """The Edit Chat Buttons dialog shows the saved buttons straight after a save."""
    spec = sections.section_spec("chat_buttons")
    assert spec is sections.CHAT_BUTTONS
    assert spec.cache_tier is sections.CacheTier.IMMEDIATE


def test_registering_the_same_section_twice_with_the_same_policy_is_allowed() -> None:
    """Re-importing a module must not fail."""
    spec = sections.SUMMARY_PROMPT_TEMPLATES
    assert sections.register(spec) is spec


def test_registering_a_second_policy_for_one_section_is_refused() -> None:
    """Two declarations would mean the applied policy depended on import order."""
    with pytest.raises(ValueError):
        sections.register(
            sections.SectionSpec(
                name="summary_prompt_templates",
                cache_tier=sections.CacheTier.IMMEDIATE,
                description="a second, contradictory declaration",
            )
        )


def test_no_read_api_accepts_a_caching_argument() -> None:
    """The policy comes from the registry, never from the caller.

    Checked by reading the signatures rather than by convention: this is the one
    property that stops the design regressing to a policy per call site.
    """
    forbidden = {"ttl", "ttl_seconds", "cache", "cache_ttl", "cache_tier", "max_age", "no_cache"}
    for name in ("get_section", "get_document", "read_record", "read_profile"):
        parameters = set(inspect.signature(getattr(store.ConfigurationStore, name)).parameters)
        assert not parameters & forbidden, (
            f"ConfigurationStore.{name} takes {sorted(parameters & forbidden)}, which "
            "would let a call site choose its own caching policy"
        )


def test_the_asr_engine_section_is_on_the_short_tier() -> None:
    """The transcriber's reader has always cached these switches for 30 seconds.

    A burst of meetings starting together shares one read; a change reaches every
    meeting that starts more than 30 seconds after it was saved.
    """
    spec = sections.section_spec("asr_engine")
    assert spec is sections.ASR_ENGINE
    assert spec.cache_tier is sections.CacheTier.SHORT
