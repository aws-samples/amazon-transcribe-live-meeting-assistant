#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""The two halves of the configuration accessor must describe one store.

The accessor exists twice -- Python for the Lambda functions, and TypeScript in
the WebSocket transcriber's package, where nothing reads it yet (the Virtual
Participant is a separate package and will need the module moved or copied).
Twice, because those run in different runtimes and LMA has no mechanism for
sharing code between them. That is a duplication with a specific failure mode:
the halves drift, and the one that drifts reads the other's records as empty or
as stale. Nothing at runtime notices, because an empty configuration is
indistinguishable from a configuration nobody has set.

So the shared facts are asserted here, across the language boundary, by reading
the TypeScript source as text:

* the partition key, the body attribute and the closed metadata list,
* the key prefixes and the two keys the store owns,
* the three cache tiers, their TTLs and which one reads consistently.

A TTL that matched on one side and not the other would mean a value the UI
promises applies at once being served stale to one of the two consumers, which
is the whole thing the tier design is for.

This runs with `make test-ai-stack` and needs no AWS and no npm install.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PYTHON_HALF = REPO_ROOT / "lma-ai-stack" / "source" / "lambda_layers" / "configuration_layer"
TYPESCRIPT_HALF = (
    REPO_ROOT
    / "lma-websocket-transcriber-stack"
    / "source"
    / "app"
    / "src"
    / "config"
    / "configuration-store.ts"
)

if str(PYTHON_HALF) not in sys.path:
    sys.path.insert(0, str(PYTHON_HALF))

from lma_config import codec, keys, sections  # noqa: E402


@pytest.fixture(scope="module")
def typescript() -> str:
    assert TYPESCRIPT_HALF.is_file(), f"{TYPESCRIPT_HALF} is not there"
    return TYPESCRIPT_HALF.read_text(encoding="utf-8")


def _string_constant(source: str, name: str) -> str:
    match = re.search(rf"export const {name} = '([^']*)'", source)
    assert match, f"the TypeScript half does not export a string constant {name}"
    return match.group(1)


def _number_in_record(source: str, record: str, key: str) -> float:
    """One entry of an exported ``Record`` literal, e.g. ``TIER_TTL_MS``."""
    body = re.search(rf"export const {record}[^=]*=\s*\{{(.*?)\}};", source, re.S)
    assert body, f"the TypeScript half does not export {record}"
    match = re.search(rf"{key}:\s*([0-9_]+)", body.group(1))
    assert match, f"{record} in the TypeScript half has no entry for {key}"
    return float(match.group(1).replace("_", ""))


def _boolean_in_record(source: str, record: str, key: str) -> bool:
    body = re.search(rf"export const {record}[^=]*=\s*\{{(.*?)\}};", source, re.S)
    assert body, f"the TypeScript half does not export {record}"
    match = re.search(rf"{key}:\s*(true|false)", body.group(1))
    assert match, f"{record} in the TypeScript half has no entry for {key}"
    return match.group(1) == "true"


# ── the item layout ─────────────────────────────────────────────────────────


def test_both_halves_agree_on_the_environment_variable_naming_the_table(
    typescript: str,
) -> None:
    """One spelling. Two names for one table gets confusing quickly."""
    from lma_config.store import TABLE_NAME_ENV_VAR

    assert _string_constant(typescript, "TABLE_NAME_ENV_VAR") == TABLE_NAME_ENV_VAR


def test_both_halves_agree_on_the_partition_key_and_the_body_attribute(
    typescript: str,
) -> None:
    """Reading the wrong attribute name reads nothing, with no error."""
    assert _string_constant(typescript, "KEY_ATTRIBUTE") == codec.KEY_ATTRIBUTE
    assert _string_constant(typescript, "BODY_ATTRIBUTE") == codec.BODY_ATTRIBUTE


def test_both_halves_agree_on_the_closed_metadata_list(typescript: str) -> None:
    """An attribute on one list and not the other lands in the body on one side.

    It would then be compressed by one half and read as a top-level attribute by
    the other, which shows up as a configuration key appearing and disappearing
    depending on which service last wrote the record.
    """
    declared = re.search(
        r"export const METADATA_ATTRIBUTES[^=]*=\s*new Set\(\[(.*?)\]\)", typescript, re.S
    )
    assert declared, "the TypeScript half does not export METADATA_ATTRIBUTES"
    names = re.findall(r"'([^']+)'", declared.group(1))
    # The first entry is the key attribute, spelled through its own constant.
    names = [codec.KEY_ATTRIBUTE if name == "KEY_ATTRIBUTE" else name for name in names]
    assert tuple(names) == codec.METADATA_ATTRIBUTES[1:], (
        "the metadata lists differ between the two halves of the accessor"
    )


def test_both_halves_agree_on_the_key_prefixes(typescript: str) -> None:
    assert _string_constant(typescript, "CONFIG_PREFIX") == keys.CONFIG_PREFIX
    for name, expected in (
        ("DEFAULT_KEY", keys.DEFAULT_KEY),
        ("ACTIVE_POINTER_KEY", keys.ACTIVE_POINTER_KEY),
    ):
        match = re.search(rf"export const {name} = `\$\{{CONFIG_PREFIX\}}([^`]*)`", typescript)
        assert match, f"the TypeScript half does not export {name}"
        assert f"{keys.CONFIG_PREFIX}{match.group(1)}" == expected


# ── the cache tiers ─────────────────────────────────────────────────────────


def test_both_halves_declare_the_same_three_tiers(typescript: str) -> None:
    declared = re.search(r"export type CacheTier =([^;]*);", typescript)
    assert declared, "the TypeScript half does not export CacheTier"
    assert re.findall(r"'([^']+)'", declared.group(1)) == [
        tier.value for tier in sections.CacheTier
    ]


@pytest.mark.parametrize("tier", list(sections.CacheTier), ids=lambda tier: tier.value)
def test_both_halves_agree_on_the_ttl_of_each_tier(
    tier: sections.CacheTier, typescript: str
) -> None:
    """Asserted in milliseconds against seconds, so a unit slip is caught too."""
    assert _number_in_record(typescript, "TIER_TTL_MS", tier.value) == (
        sections.TIER_TTL_SECONDS[tier] * 1000
    )


@pytest.mark.parametrize("tier", list(sections.CacheTier), ids=lambda tier: tier.value)
def test_both_halves_agree_on_which_tiers_read_consistently(
    tier: sections.CacheTier, typescript: str
) -> None:
    assert (
        _boolean_in_record(typescript, "TIER_CONSISTENT_READ", tier.value)
        == (sections.TIER_CONSISTENT_READ[tier])
    )


def test_the_immediate_tier_is_no_cache_on_both_sides(typescript: str) -> None:
    """The one tier whose value is a promise rather than a tuning choice.

    A non-zero TTL here would mean a window in which a withdrawal of consent
    had not taken effect, which is the thing the tier exists to rule out.
    """
    assert sections.TIER_TTL_SECONDS[sections.CacheTier.IMMEDIATE] == 0
    assert _number_in_record(typescript, "TIER_TTL_MS", "immediate") == 0
    assert sections.TIER_CONSISTENT_READ[sections.CacheTier.IMMEDIATE] is True
    assert _boolean_in_record(typescript, "TIER_CONSISTENT_READ", "immediate") is True


def test_neither_half_offers_a_caching_argument_on_a_read(typescript: str) -> None:
    """The policy comes from the section, never from the caller.

    The Python half's signatures are checked by its own test suite; this is the
    TypeScript side of the same property, and it is what stops the design
    regressing to the four disagreeing policies it replaces.
    """
    signature = re.search(r"async getSection\b(.*?)\): Promise", typescript, re.S)
    assert signature, "the TypeScript half does not declare getSection"
    assert "ttl" not in signature.group(1).lower()
    assert "cache" not in signature.group(1).lower()
    assert "spec: SectionSpec" in signature.group(1), (
        "getSection should take a section declaration, so the tier travels with it"
    )


# ── the sections both halves know about ─────────────────────────────────────


def test_both_halves_permit_the_same_profile_names(typescript: str) -> None:
    """A name one half accepts and the other does not is a record one cannot read.

    The name goes into the partition key, so the two halves disagreeing about
    which names are legal means one of them resolving the active-profile pointer
    to a profile and the other resolving it to the baseline -- a deployment
    where two services read different configuration and neither reports a
    problem.
    """
    declared = re.search(r"export const PROFILE_NAME = /([^/]+)/", typescript)
    assert declared, "the TypeScript half does not export PROFILE_NAME"
    assert declared.group(1) == keys.PROFILE_NAME_PATTERN.pattern, (
        "the two halves permit different profile names"
    )

    reserved = re.search(
        r"export const RESERVED_PROFILE_NAMES[^=]*=\s*new Set\(\[(.*?)\]\)", typescript, re.S
    )
    assert reserved, "the TypeScript half does not export RESERVED_PROFILE_NAMES"
    assert set(re.findall(r"'([^']+)'", reserved.group(1))) == set(keys.RESERVED_PROFILE_NAMES), (
        "the two halves reserve different names"
    )


def test_both_halves_pin_the_same_encoded_body_fixture() -> None:
    """One copy of the compressed bytes, committed on both sides.

    The TypeScript half reads and never writes, so a round-trip in either
    language says nothing about whether it can read what the Python half
    produces. A fixture of real bytes does, and keeping the same base64 in both
    suites is what stops one side's copy being regenerated without the other's.
    """
    python_suite = (PYTHON_HALF / "lma_config" / "test_codec.py").read_text(encoding="utf-8")
    typescript_suite = (TYPESCRIPT_HALF.parent / "configuration-store.test.ts").read_text(
        encoding="utf-8"
    )

    def base64_blobs(source: str) -> set[str]:
        # Concatenated across lines in the TypeScript copy and across string
        # literals in the Python one, so the quotes come out before comparison.
        chunks = re.findall(r"H4sI[A-Za-z0-9+/=]*(?:['\"]\s*\+?\s*['\"][A-Za-z0-9+/=]*)*", source)
        return {re.sub(r"['\"\s+]", "", chunk) for chunk in chunks}

    python_blobs = base64_blobs(python_suite)
    typescript_blobs = base64_blobs(typescript_suite)
    assert python_blobs, "the Python suite pins no encoded-body fixture"
    assert typescript_blobs, "the TypeScript suite pins no encoded-body fixture"
    assert python_blobs == typescript_blobs, (
        f"the two halves pin different encoded bodies: {python_blobs} vs {typescript_blobs}"
    )


def test_both_halves_pin_the_same_fingerprint_vector() -> None:
    """Each suite digests one fixed input; the expected digest must be one value.

    Neither suite can run the other half, so a pinned vector is what keeps the
    two digests agreeing. If they diverged, a caller comparing a fingerprint
    taken by one with one taken by the other would rebuild its derived state on
    every read, or never rebuild it at all -- and neither failure announces
    itself. The vector is deliberately non-ASCII, because that is where two JSON
    serialisers disagree by default, and the shipped prompts contain typographic
    quotes and a degree sign.
    """
    python_suite = (PYTHON_HALF / "lma_config" / "test_store.py").read_text(encoding="utf-8")
    typescript_suite = (TYPESCRIPT_HALF.parent / "configuration-store.test.ts").read_text(
        encoding="utf-8"
    )
    digests = {}
    for name, source in (("python", python_suite), ("typescript", typescript_suite)):
        found = re.findall(r"FINGERPRINT_VECTOR_DIGEST\s*[:=]\s*'?\"?([0-9a-f]{16})", source)
        assert found, f"the {name} suite pins no FINGERPRINT_VECTOR_DIGEST"
        digests[name] = set(found)
    assert digests["python"] == digests["typescript"], (
        f"the two halves pin different fingerprint digests: {digests}"
    )


def test_a_section_declared_in_both_halves_declares_the_same_tier(typescript: str) -> None:
    """A section cached for longer on one side than the other is the drift.

    Only sections named in both are compared: the halves are migrated one store
    at a time, so one knowing about a section the other does not is the expected
    state in between.
    """
    declared_in_typescript = {
        name: tier
        for name, tier in re.findall(
            r"declareSection\(\{\s*name: '([^']+)',\s*cacheTier: '([^']+)'", typescript
        )
    }
    assert declared_in_typescript, "no sections are declared in the TypeScript half"
    shared = set(declared_in_typescript) & set(sections.registered_sections())
    assert shared, "the two halves declare no section in common"
    for name in sorted(shared):
        assert declared_in_typescript[name] == sections.section_spec(name).cache_tier.value, (
            f"the section {name!r} is on a different cache tier in each half"
        )
