#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Tests for the shared configuration accessor.

Covered here, in order of how much damage each would do if wrong:

* **A stack update must not overwrite an operator's edits.** The seeder runs on
  every create and every update; the only thing standing between it and a
  deployment that resets the prompt templates an admin wrote is the conditional
  write in ``seed_profile``. Asserted by running the whole seed sequence twice.
* **Two admin sessions must not silently discard each other's work.** Every
  existing LMA configuration store writes whole items with no condition, so the
  second save wins and the first is gone with no trace. ``write_profile``
  refuses instead, and both the pre-check and the condition on the write itself
  are tested, because the pre-check alone would still lose a write that landed
  between the read and the save.
* **The declared cache tier, not the call site, decides staleness.** The
  immediate tier must never serve a cached value and must read consistently;
  the short and standard tiers must actually cache, since the point of the TTL
  is to keep a burst of concurrent meetings from each doing its own read.

DynamoDB is a fake that implements ``get_item``/``put_item`` over a dict,
including ``ConditionExpression`` for the two forms the store uses. It records
every request so the tests can assert on ``ConsistentRead`` and on the
conditions actually sent, rather than on the accessor's own report of what it
did.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest
from botocore.exceptions import ClientError

from lma_config import codec, sections
from lma_config.errors import (
    ConcurrentModification,
    ConfigurationError,
    ConfigurationTableNotConfigured,
    InvalidConfiguration,
    ReservedProfileName,
    UnknownConfigurationSection,
)
from lma_config.sections import CacheTier
from lma_config.store import ConfigurationStore, deep_merge

TABLE = "lma-configuration"
DEFAULT_BODY = {
    "summary_prompt_templates": {
        "1#SUMMARY": "shipped summary prompt",
        "2#ACTIONS": "shipped actions prompt",
    }
}


class FakeDynamo:
    """``get_item``/``put_item`` over a dict, with the two conditions in use."""

    def __init__(self) -> None:
        self.items: Dict[str, Dict[str, Any]] = {}
        self.requests: List[Dict[str, Any]] = []

    def get_item(self, **request: Any) -> Dict[str, Any]:
        self.requests.append({"op": "get_item", **request})
        key = request["Key"][codec.KEY_ATTRIBUTE]["S"]
        item = self.items.get(key)
        return {"Item": item} if item is not None else {}

    def put_item(self, **request: Any) -> Dict[str, Any]:
        self.requests.append({"op": "put_item", **request})
        item = request["Item"]
        key = item[codec.KEY_ATTRIBUTE]["S"]
        condition = request.get("ConditionExpression")
        if condition == "attribute_not_exists(#key)" and key in self.items:
            raise self._refused()
        if condition == "attribute_not_exists(#version)" and "Version" in self.items.get(key, {}):
            raise self._refused()
        if condition == "#version = :expected":
            expected = request["ExpressionAttributeValues"][":expected"]["N"]
            stored = self.items.get(key, {}).get("Version", {}).get("N")
            if stored != expected:
                raise self._refused()
        self.items[key] = item
        return {}

    @staticmethod
    def _refused() -> ClientError:
        return ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException", "Message": "refused"}},
            "PutItem",
        )

    # ── helpers the tests read the fake through ────────────────────────────

    def body(self, key: str) -> Dict[str, Any]:
        return codec.decode_body(self.items[key][codec.BODY_ATTRIBUTE]["B"])

    def metadata(self, key: str) -> Dict[str, Any]:
        return codec.split_item(codec.unmarshal_item(self.items[key]))[0]

    def puts(self) -> List[Dict[str, Any]]:
        return [request for request in self.requests if request["op"] == "put_item"]

    def gets(self) -> List[Dict[str, Any]]:
        return [request for request in self.requests if request["op"] == "get_item"]


class Clock:
    """A monotonic clock the tests advance by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def build(
    *, clock: Optional[Clock] = None, timestamps: Optional[List[str]] = None
) -> tuple[ConfigurationStore, FakeDynamo, Clock]:
    dynamo = FakeDynamo()
    the_clock = clock or Clock()
    issued = iter(timestamps or [])

    def now() -> str:
        return next(issued, "2026-01-01T00:00:00Z")

    return (
        ConfigurationStore(TABLE, client=dynamo, clock=the_clock, now=now),
        dynamo,
        the_clock,
    )


def _register_for_this_test(monkeypatch: pytest.MonkeyPatch, name: str, tier: CacheTier) -> None:
    """Add a section for the duration of one test.

    The two tiers nothing in this stage populates still have to be exercised, or
    the design's central claim -- that the policy is per section -- would rest on
    one tier's behaviour. Registered through monkeypatch so the real registry is
    not left with a test section in it.
    """
    monkeypatch.setitem(
        sections._REGISTRY,  # noqa: SLF001 -- same package; the registry is the thing under test
        name,
        sections.SectionSpec(name, tier, f"a {tier.value}-tier section used by the tests"),
    )


def seed(store: ConfigurationStore) -> None:
    """What the deploy-time seeder does, in the order it does it."""
    store.write_default(DEFAULT_BODY)
    store.seed_profile("custom", {})
    store.set_active_profile("custom", only_if_absent=True)


# ── the table name ─────────────────────────────────────────────────────────


def test_the_table_name_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    """A store built against no table would read empty configuration forever."""
    monkeypatch.delenv("CONFIGURATION_TABLE_NAME", raising=False)
    with pytest.raises(ConfigurationTableNotConfigured):
        ConfigurationStore(client=FakeDynamo())


def test_the_table_name_comes_from_one_environment_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One spelling, in every template and every handler."""
    monkeypatch.setenv("CONFIGURATION_TABLE_NAME", "from-the-environment")
    assert ConfigurationStore(client=FakeDynamo()).table_name == "from-the-environment"


# ── reading ────────────────────────────────────────────────────────────────


def test_a_profile_is_a_delta_over_the_shipped_baseline() -> None:
    """Only the keys an operator changed are stored, and the rest still ship."""
    store, dynamo, _ = build()
    seed(store)
    store.write_profile(
        "custom",
        {"summary_prompt_templates": {"1#SUMMARY": "my own summary prompt"}},
        updated_by="admin@example.com",
        expected_version=1,
    )

    section = store.get_section("summary_prompt_templates")
    assert section.values == {
        "1#SUMMARY": "my own summary prompt",
        "2#ACTIONS": "shipped actions prompt",
    }
    assert section.active_profile == "custom"
    assert dynamo.body("Config#custom") == {
        "summary_prompt_templates": {"1#SUMMARY": "my own summary prompt"}
    }


def test_the_baseline_alone_is_served_when_no_profile_has_been_written() -> None:
    store, _, _ = build()
    store.write_default(DEFAULT_BODY)
    assert (
        store.get_section("summary_prompt_templates").values
        == (DEFAULT_BODY["summary_prompt_templates"])
    )


def test_a_pointer_naming_a_profile_that_is_not_there_falls_back_to_the_baseline() -> None:
    """A deleted profile must not take the summaries down with it."""
    store, _, _ = build()
    store.write_default(DEFAULT_BODY)
    store.set_active_profile("gone")
    section = store.get_section("summary_prompt_templates")
    assert section.values == DEFAULT_BODY["summary_prompt_templates"]
    assert section.active_profile is None


@pytest.mark.parametrize(
    "pointer", ["not a name", "../escape", "default", "__active", "schema", ""]
)
def test_a_pointer_naming_something_that_is_not_a_profile_falls_back(pointer: str) -> None:
    """One hand-edited attribute must not stop every read in the deployment.

    Raising would be the natural thing for an invalid name, and the wrong thing
    here: for the prompt templates it would mean no summaries at all. The answer
    is the same as for a pointer naming a profile that is not stored -- the
    baseline -- and the TypeScript half resolves it the same way, which the
    cross-language contract test asserts.
    """
    store, dynamo, _ = build()
    store.write_default(DEFAULT_BODY)
    dynamo.items["Config#__active"] = {
        "Configuration": {"S": "Config#__active"},
        "ActiveProfile": {"S": pointer},
    }
    section = store.get_section("summary_prompt_templates")
    assert section.values == DEFAULT_BODY["summary_prompt_templates"]
    assert section.active_profile is None
    assert store.active_profile_name() is None


def test_a_record_written_before_this_layout_is_read_as_it_stands() -> None:
    """Pointing the accessor at an existing table needs no migration step.

    The item here has no compressed body and its values at the top level, which
    is how every configuration record in LMA was written until now.
    """
    store, dynamo, _ = build()
    dynamo.items["Config#default"] = {
        "Configuration": {"S": "Config#default"},
        "summary_prompt_templates": {"M": {"1#SUMMARY": {"S": "an inline prompt"}}},
    }
    assert store.get_section("summary_prompt_templates").values == {"1#SUMMARY": "an inline prompt"}


def test_a_section_the_document_does_not_hold_reads_as_empty() -> None:
    store, _, _ = build()
    store.write_default({})
    assert store.get_section("summary_prompt_templates").values == {}


def test_a_section_holding_something_other_than_an_object_reads_as_empty() -> None:
    """A hand edit in the console cannot make the reader raise mid-summary."""
    store, _, _ = build()
    store.write_default({"summary_prompt_templates": "not an object"})
    assert store.get_section("summary_prompt_templates").values == {}


# The same values and the same expected digest appear in the TypeScript half's
# suite. Neither side can run the other, so this pinned vector is what keeps the
# two implementations agreeing: a caller that compared a fingerprint taken by one
# with one taken by the other would otherwise rebuild its derived state forever,
# or never.
#
# The vector holds the two things two JSON serialisers disagree about by default,
# because a vector of plain ASCII strings would agree whatever either half did:
#
# * Non-ASCII text, which Python escapes and JavaScript does not. The shipped
#   prompts contain typographic quotes and a degree sign.
# * An integral float, which Python writes as `1.0` and JavaScript as `1`. It
#   cannot come from the UI, but it can come from a body a later store migration
#   writes -- and the tier that will carry numeric limits is the one the
#   fingerprint exists for.
FINGERPRINT_VECTOR = {
    "1#Summary": "Write a summary. Use ’ quotes, 20°, and “this”.",
    "2#List": ["a", 1, True],
    "3#Nested": {"z": None, "a": 1.5},
    "4#Numbers": {"integral": 1.0, "whole": 512, "fraction": 0.25, "negative": -3.0},
}
FINGERPRINT_VECTOR_DIGEST = "d24206f131a4a1ad"


def test_the_fingerprint_matches_the_typescript_halfs_for_a_pinned_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both halves must digest the same section to the same string."""
    _register_for_this_test(monkeypatch, "fingerprint_vector", CacheTier.STANDARD)
    store, _, _ = build()
    store.write_default({"fingerprint_vector": FINGERPRINT_VECTOR})
    assert store.get_section("fingerprint_vector").fingerprint == FINGERPRINT_VECTOR_DIGEST


def test_the_fingerprint_changes_only_when_the_section_does() -> None:
    """What lets a reader skip rebuilding work derived from the configuration."""
    store, _, clock = build()
    seed(store)
    first = store.get_section("summary_prompt_templates").fingerprint

    clock.advance(3600)
    assert store.get_section("summary_prompt_templates").fingerprint == first

    store.write_profile(
        "custom",
        {"summary_prompt_templates": {"1#SUMMARY": "changed"}},
        updated_by="admin@example.com",
        expected_version=1,
    )
    assert store.get_section("summary_prompt_templates").fingerprint != first


# ── the declared cache tiers ───────────────────────────────────────────────


def test_the_standard_tier_serves_a_cached_document_within_its_ttl() -> None:
    """Why the tier exists: one read per 300 s, not one per summary."""
    store, dynamo, clock = build()
    seed(store)
    store.invalidate()

    assert store.get_section("summary_prompt_templates").served_from_cache is False
    reads_after_first = len(dynamo.gets())

    clock.advance(299)
    assert store.get_section("summary_prompt_templates").served_from_cache is True
    assert len(dynamo.gets()) == reads_after_first, "a cached read still went to DynamoDB"

    clock.advance(2)
    assert store.get_section("summary_prompt_templates").served_from_cache is False
    assert len(dynamo.gets()) > reads_after_first


def test_a_document_with_no_baseline_is_never_cached() -> None:
    """The window between a stack update and the seeder finishing.

    New code can be live before `Config#default` exists. Caching the empty
    document that produces would make every reader answer from it for the whole
    of the tier's TTL *after* seeding had finished -- so a few seconds of a
    missing baseline would become five minutes of failing summaries. Reading
    again each time until the baseline appears makes the window as short as it
    can be, and the read that finds it is cached as normal.
    """
    store, dynamo, clock = build()
    reads = len(dynamo.gets())
    store.get_section("summary_prompt_templates")
    assert len(dynamo.gets()) > reads

    reads = len(dynamo.gets())
    store.get_section("summary_prompt_templates")
    assert len(dynamo.gets()) > reads, "the empty document was served from the cache"

    # The seeder lands. The very next read picks it up, with no TTL to wait out.
    store.write_default(DEFAULT_BODY)
    store.invalidate()
    section = store.get_section("summary_prompt_templates")
    assert section.values == DEFAULT_BODY["summary_prompt_templates"]
    reads = len(dynamo.gets())
    clock.advance(1)
    assert store.get_section("summary_prompt_templates").served_from_cache is True
    assert len(dynamo.gets()) == reads


def test_the_standard_tier_does_not_read_consistently() -> None:
    """Paying for consistency under a 300 s TTL would buy nothing.

    The refetch is forced by letting the TTL lapse rather than by calling
    `invalidate`, because `invalidate` arms a consistent read on purpose. This
    is about the tier's own policy, which applies to the reads it makes of its
    own accord; the writes `seed` performs read consistently for a different
    reason.
    """
    store, dynamo, clock = build()
    seed(store)
    store.get_section("summary_prompt_templates")
    clock.advance(301)
    dynamo.requests.clear()
    store.get_section("summary_prompt_templates")
    assert dynamo.gets(), "nothing was read"
    assert all(request["ConsistentRead"] is False for request in dynamo.gets())


def test_the_immediate_tier_never_serves_a_cached_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A value promised to apply at once has no window in which it does not.

    This is the voice-enrollment reader's policy, which is deliberately the
    opposite of the ASR switches' in the same directory: a withdrawal of consent
    must hold for every later meeting, and a cache is exactly a window in which
    it would not.
    """
    _register_for_this_test(monkeypatch, "consent_bearing", CacheTier.IMMEDIATE)
    store, dynamo, clock = build()
    store.write_default({"consent_bearing": {"enrolled": True}})

    first = store.get_section("consent_bearing")
    reads = len(dynamo.gets())
    assert first.served_from_cache is False

    # No time passes at all: still a fresh read.
    second = store.get_section("consent_bearing")
    assert second.served_from_cache is False
    assert len(dynamo.gets()) > reads


def test_the_immediate_tier_reads_consistently(monkeypatch: pytest.MonkeyPatch) -> None:
    _register_for_this_test(monkeypatch, "consent_bearing", CacheTier.IMMEDIATE)
    store, dynamo, _ = build()
    store.write_default({"consent_bearing": {"enrolled": True}})
    dynamo.requests.clear()
    store.get_section("consent_bearing")
    assert dynamo.gets(), "nothing was read"
    assert all(request["ConsistentRead"] is True for request in dynamo.gets())


def test_the_short_tier_caches_for_thirty_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    """The transcriber's existing ASR-switch policy, preserved exactly."""
    _register_for_this_test(monkeypatch, "per_meeting_switch", CacheTier.SHORT)
    store, _, clock = build()
    store.write_default({"per_meeting_switch": {"engine": "microvm"}})
    store.invalidate()

    assert store.get_section("per_meeting_switch").served_from_cache is False
    clock.advance(29)
    assert store.get_section("per_meeting_switch").served_from_cache is True
    clock.advance(2)
    assert store.get_section("per_meeting_switch").served_from_cache is False


def test_the_read_after_an_invalidate_is_consistent_once() -> None:
    """The one case the tiers' eventual-consistency choice does not cover.

    That choice rests on a TTL measured in tens of seconds being far longer
    than DynamoDB's replication lag. After an invalidate there is no TTL: the
    read happens immediately after the thing it has to observe -- a save the
    caller just made, or the save an administrator made a moment before pressing
    Regenerate. One consistent read, then back to the tier's own policy.
    """
    store, dynamo, clock = build()
    seed(store)
    store.invalidate()
    dynamo.requests.clear()
    store.get_section("summary_prompt_templates")
    assert dynamo.gets(), "nothing was read"
    assert all(request["ConsistentRead"] is True for request in dynamo.gets())

    # One-shot: the next refetch of its own accord is the tier's policy again.
    clock.advance(301)
    dynamo.requests.clear()
    store.get_section("summary_prompt_templates")
    assert dynamo.gets(), "nothing was read"
    assert all(request["ConsistentRead"] is False for request in dynamo.gets())


def test_a_write_makes_the_writers_own_next_read_see_it() -> None:
    """Otherwise the resolver would answer the save with the previous values."""
    store, _, clock = build()
    seed(store)
    store.get_section("summary_prompt_templates")
    store.write_profile(
        "custom",
        {"summary_prompt_templates": {"1#SUMMARY": "just saved"}},
        updated_by="admin@example.com",
        expected_version=1,
    )
    assert store.get_section("summary_prompt_templates").values["1#SUMMARY"] == "just saved"


# ── a stack update must not overwrite operator edits ───────────────────────


def test_a_repeated_deployment_leaves_an_operators_profile_alone() -> None:
    """The property the deploy-time seeder exists to have.

    `seed_profile` runs on every stack create and every stack update. The
    second run here stands for the next deployment of the same stack.
    """
    store, dynamo, _ = build()
    seed(store)
    store.write_profile(
        "custom",
        {"summary_prompt_templates": {"1#SUMMARY": "the prompt an admin wrote"}},
        updated_by="admin@example.com",
        expected_version=1,
    )

    seed(store)  # the next deployment

    assert dynamo.body("Config#custom") == {
        "summary_prompt_templates": {"1#SUMMARY": "the prompt an admin wrote"}
    }
    assert dynamo.metadata("Config#custom")["UpdatedBy"] == "admin@example.com"
    assert store.get_section("summary_prompt_templates").values["1#SUMMARY"] == (
        "the prompt an admin wrote"
    )


def test_the_baselines_version_advances_with_each_deployment() -> None:
    """Otherwise the field looks like information and is not.

    It is what a diff against the baseline would use to tell one deployment's
    shipped content from the next.
    """
    store, dynamo, _ = build()
    store.write_default(DEFAULT_BODY)
    assert dynamo.metadata("Config#default")["Version"] == 1
    store.write_default(DEFAULT_BODY)
    assert dynamo.metadata("Config#default")["Version"] == 2
    assert store.get_document().default_version == 2


def test_a_repeated_deployment_does_rewrite_the_baseline() -> None:
    """The baseline is the deployment's own, so a new release must land."""
    store, dynamo, _ = build()
    seed(store)
    store.write_default({"summary_prompt_templates": {"1#SUMMARY": "a newer shipped prompt"}})
    assert dynamo.body("Config#default") == {
        "summary_prompt_templates": {"1#SUMMARY": "a newer shipped prompt"}
    }


def test_seeding_reports_whether_it_created_the_profile() -> None:
    """The seeder logs this, so an operator can see which branch ran."""
    store, _, _ = build()
    assert store.seed_profile("custom", {}) is True
    assert store.seed_profile("custom", {}) is False


def test_a_section_can_be_added_to_a_profile_that_already_exists() -> None:
    """A store moving in after the profile was created still lands in it."""
    store, dynamo, _ = build()
    seed(store)
    store.write_section(
        "custom",
        "summary_prompt_templates",
        {"1#SUMMARY": "the prompt an admin wrote"},
        updated_by="admin@example.com",
        expected_version=None,
    )

    assert store.seed_section("custom", "nova_sonic_config", {"voiceId": "matthew"}) is True

    assert dynamo.body("Config#custom") == {
        "summary_prompt_templates": {"1#SUMMARY": "the prompt an admin wrote"},
        "nova_sonic_config": {"voiceId": "matthew"},
    }
    assert dynamo.metadata("Config#custom")["Version"] == 3


def test_a_seeded_section_is_never_replaced_by_a_later_run() -> None:
    """Presence is the guard, so a later deployment cannot resurrect old values."""
    store, dynamo, _ = build()
    seed(store)
    store.seed_section("custom", "nova_sonic_config", {"voiceId": "matthew"})
    store.write_section(
        "custom",
        "nova_sonic_config",
        {"voiceId": "amy"},
        updated_by="admin@example.com",
        expected_version=None,
    )

    assert store.seed_section("custom", "nova_sonic_config", {"voiceId": "matthew"}) is False
    assert dynamo.body("Config#custom")["nova_sonic_config"] == {"voiceId": "amy"}


def test_an_empty_seeded_section_counts_as_present() -> None:
    """What the seeder writes when there was nothing to carry across.

    Equally what a page's "reset to defaults" saves. Either way, the next run
    must leave it alone rather than read the previous table again.
    """
    store, dynamo, _ = build()
    seed(store)
    assert store.seed_section("custom", "nova_sonic_config", {}) is True
    assert store.seed_section("custom", "nova_sonic_config", {"voiceId": "matthew"}) is False
    assert dynamo.body("Config#custom")["nova_sonic_config"] == {}


def test_seeding_a_section_into_a_missing_profile_writes_nothing() -> None:
    store, dynamo, _ = build()
    store.write_default(DEFAULT_BODY)
    assert store.seed_section("custom", "nova_sonic_config", {"voiceId": "matthew"}) is False
    assert "Config#custom" not in dynamo.items


def test_seeding_a_section_is_conditional_on_the_record_it_read() -> None:
    """A save landing between the read and the write must not be overwritten."""
    store, dynamo, _ = build()
    seed(store)
    dynamo.requests.clear()

    store.seed_section("custom", "nova_sonic_config", {})

    (put,) = dynamo.puts()
    assert put["ConditionExpression"] == "#version = :expected"
    assert put["ExpressionAttributeValues"] == {":expected": {"N": "1"}}


def test_seeding_a_section_keeps_how_the_profile_came_to_exist() -> None:
    """ProfileOrigin is what the stack output reports; a seeder run must keep it."""
    store, dynamo, _ = build()
    store.write_default(DEFAULT_BODY)
    store.seed_profile("custom", {}, origin="created-from-previous-table")
    store.seed_section("custom", "nova_sonic_config", {})
    assert dynamo.metadata("Config#custom")["ProfileOrigin"] == "created-from-previous-table"


def test_seeding_an_unregistered_section_is_refused() -> None:
    store, _, _ = build()
    seed(store)
    with pytest.raises(UnknownConfigurationSection):
        store.seed_section("custom", "not_a_section", {})


def test_a_repeated_deployment_leaves_the_active_pointer_where_an_operator_put_it() -> None:
    store, dynamo, _ = build()
    seed(store)
    store.set_active_profile("production")
    assert store.set_active_profile("custom", only_if_absent=True) is False
    assert dynamo.metadata("Config#__active")["ActiveProfile"] == "production"


# ── concurrent saves ───────────────────────────────────────────────────────


# ── saving one section of a profile ────────────────────────────────────────


def test_saving_one_section_leaves_the_others_alone() -> None:
    """The failure this method exists to prevent, and it is a silent one.

    One record holds every section an operator has changed. A page that
    replaced the whole body would delete the settings belonging to every other
    page -- invisible while one section is registered, and invisible again on
    the day a second store moves in, because nothing fails: the other page just
    reads the baseline and shows the shipped values back.
    """
    store, dynamo, _ = build()
    seed(store)
    store.write_section(
        "custom",
        "asr_engine",
        {"streamingEngineMicrovm": True},
        updated_by="admin@example.com",
        expected_version=1,
    )
    store.write_section(
        "custom",
        "summary_prompt_templates",
        {"1#SUMMARY": "my prompt"},
        updated_by="admin@example.com",
        expected_version=2,
    )

    assert dynamo.body("Config#custom") == {
        "asr_engine": {"streamingEngineMicrovm": True},
        "summary_prompt_templates": {"1#SUMMARY": "my prompt"},
    }
    assert store.get_section("asr_engine").values == {"streamingEngineMicrovm": True}


def test_saving_a_section_replaces_it_rather_than_merging_its_keys() -> None:
    """A key an operator removed has to be able to go, and absent says that."""
    store, dynamo, _ = build()
    seed(store)
    store.write_section(
        "custom",
        "summary_prompt_templates",
        {"1#A": "a", "2#B": "b"},
        updated_by="admin@example.com",
        expected_version=1,
    )
    store.write_section(
        "custom",
        "summary_prompt_templates",
        {"1#A": "a"},
        updated_by="admin@example.com",
        expected_version=2,
    )
    assert dynamo.body("Config#custom")["summary_prompt_templates"] == {"1#A": "a"}


def test_saving_an_unregistered_section_is_refused() -> None:
    """A typo would otherwise write a section nothing will ever read."""
    store, dynamo, _ = build()
    seed(store)
    dynamo.requests.clear()
    with pytest.raises(UnknownConfigurationSection):
        store.write_section(
            "custom",
            "sumary_prompt_templates",
            {"1#A": "a"},
            updated_by="admin@example.com",
            expected_version=1,
        )
    assert not dynamo.puts(), "a refused section still reached DynamoDB"


def test_a_section_that_does_not_match_its_schema_is_refused_before_anything_is_read() -> None:
    store, dynamo, _ = build()
    seed(store)
    dynamo.requests.clear()
    with pytest.raises(InvalidConfiguration) as raised:
        store.write_section(
            "custom",
            "nova_sonic_config",
            {"endpointingSensitivity": "EXTREME"},
            updated_by="admin@example.com",
            expected_version=1,
        )
    assert raised.value.problems == [
        'nova_sonic_config › endpointingSensitivity: must be one of "LOW", "MEDIUM", "HIGH"; '
        'got "EXTREME"'
    ]
    assert not dynamo.requests, "a refused save still reached DynamoDB"


def test_saving_several_sections_is_one_write_and_leaves_the_others_alone() -> None:
    store, dynamo, _ = build()
    seed(store)
    store.write_section(
        "custom",
        "summary_prompt_templates",
        {"1#SUMMARY": "mine"},
        updated_by="admin@example.com",
        expected_version=1,
    )
    dynamo.requests.clear()
    outcome = store.write_sections(
        "custom",
        {
            "asr_engine": {"streamingEngineMicrovm": True},
            "nova_sonic_config": {"voiceId": "matthew"},
        },
        updated_by="admin@example.com",
        expected_version=2,
    )
    assert outcome.version == 3
    assert len(dynamo.puts()) == 1
    assert dynamo.body("Config#custom") == {
        "summary_prompt_templates": {"1#SUMMARY": "mine"},
        "asr_engine": {"streamingEngineMicrovm": True},
        "nova_sonic_config": {"voiceId": "matthew"},
    }


def test_one_invalid_section_refuses_the_whole_save_and_reports_every_problem() -> None:
    store, dynamo, _ = build()
    seed(store)
    dynamo.requests.clear()
    with pytest.raises(InvalidConfiguration) as raised:
        store.write_sections(
            "custom",
            {
                "asr_engine": {"streamingEngineMicrovm": "yes"},
                "nova_sonic_config": {"voiceId": "matthew"},
                "call_pricing": {"regions": {"default": {"transcribe/streaming": {"seconds": -1}}}},
            },
            updated_by="admin@example.com",
            expected_version=1,
        )
    assert raised.value.problems == [
        "asr_engine › streamingEngineMicrovm: must be true or false; got text",
        "call_pricing › regions › default › transcribe/streaming › seconds: "
        "must be at least 0; got -1",
    ]
    assert not dynamo.requests


def test_a_setting_above_the_limit_the_baseline_sets_is_refused() -> None:
    """Retention may be shortened on the page and not lengthened past the stack's value."""
    store, dynamo, _ = build()
    store.write_default({**DEFAULT_BODY, "retention": {"meetingRecordExpirationInDays": 30}})
    store.seed_profile("custom", {})
    store.set_active_profile("custom", only_if_absent=True)
    with pytest.raises(InvalidConfiguration) as raised:
        store.write_sections(
            "custom",
            {"retention": {"meetingRecordExpirationInDays": 45}},
            updated_by="admin@example.com",
            expected_version=1,
        )
    assert raised.value.problems == [
        "retention › meetingRecordExpirationInDays: must be at most 30, the limit this "
        "deployment sets; got 45"
    ]
    assert dynamo.body("Config#custom") == {}

    outcome = store.write_sections(
        "custom",
        {"retention": {"meetingRecordExpirationInDays": 7.5}},
        updated_by="admin@example.com",
        expected_version=1,
    )
    assert outcome.version == 2
    assert dynamo.body("Config#custom") == {"retention": {"meetingRecordExpirationInDays": 7.5}}


def test_a_limited_setting_the_baseline_does_not_hold_is_not_refused() -> None:
    """The readers still apply the parameter's value as the limit."""
    store, _, _ = build()
    seed(store)
    outcome = store.write_sections(
        "custom",
        {"retention": {"transcriptionExpirationInDays": 400}},
        updated_by="admin@example.com",
        expected_version=1,
    )
    assert outcome.version == 2


def test_replacing_a_whole_profile_checks_every_section_in_it() -> None:
    store, dynamo, _ = build()
    seed(store)
    dynamo.requests.clear()
    with pytest.raises(InvalidConfiguration):
        store.write_profile(
            "custom",
            {"chat_buttons": {"Summarize": "no number in front"}},
            updated_by="admin@example.com",
            expected_version=1,
        )
    with pytest.raises(UnknownConfigurationSection):
        store.write_profile(
            "custom", {"not_a_section": {}}, updated_by="admin@example.com", expected_version=1
        )
    assert not dynamo.puts()


def test_the_seeder_is_not_held_to_the_schema() -> None:
    """What it adopts was accepted when it was first saved.

    Refusing it would turn a deployment into a failure. A restore is covered in
    test_revisions.py.
    """
    store, dynamo, _ = build()
    store.write_default(DEFAULT_BODY)
    assert store.seed_profile("custom", {"nova_sonic_config": {"promptMode": "legacy-value"}})
    assert store.seed_section("custom", "asr_engine", {"streamingEngineMicrovm": "true"})
    assert dynamo.body("Config#custom")["asr_engine"] == {"streamingEngineMicrovm": "true"}


def test_saving_a_section_is_conditional_like_any_other_save() -> None:
    store, dynamo, _ = build()
    seed(store)
    dynamo.requests.clear()
    store.write_section(
        "custom",
        "summary_prompt_templates",
        {"1#A": "a"},
        updated_by="first@example.com",
        expected_version=1,
    )
    assert dynamo.puts()[-1]["ConditionExpression"] == "#version = :expected"
    with pytest.raises(ConcurrentModification):
        store.write_section(
            "custom",
            "summary_prompt_templates",
            {"1#A": "b"},
            updated_by="second@example.com",
            expected_version=1,
        )


def test_saving_a_section_is_conditional_even_with_no_version_offered() -> None:
    """A read-merge-write cannot be unconditional, whatever the caller offers.

    `write_profile` lets a caller opt out of the guard, because it replaces the
    whole body and is not building it from anything. `write_section` builds its
    body *from* the record it read, so an unconditional write would replace
    whatever landed in between -- another page's save -- with a body computed
    before it existed. The version the caller offers decides only whether it
    also gets the pre-check that names who saved the record.
    """
    store, dynamo, _ = build()
    seed(store)
    dynamo.requests.clear()
    store.write_section(
        "custom",
        "summary_prompt_templates",
        {"1#A": "a"},
        updated_by="scripted",
        expected_version=None,
    )
    assert dynamo.puts()[-1]["ConditionExpression"] == "#version = :expected"
    assert dynamo.puts()[-1]["ExpressionAttributeValues"][":expected"] == {"N": "1"}


def test_replacing_a_whole_profile_may_still_opt_out_of_the_guard() -> None:
    """The counterpart: `write_profile` builds its body from nothing it read."""
    store, dynamo, _ = build()
    seed(store)
    store.write_profile("custom", {}, updated_by="scripted", expected_version=None)
    assert dynamo.puts()[-1].get("ConditionExpression") is None


def test_saving_a_section_reads_the_record_once() -> None:
    """Merging must not cost a second round trip on the save path."""
    store, dynamo, _ = build()
    seed(store)
    dynamo.requests.clear()
    store.write_section(
        "custom",
        "summary_prompt_templates",
        {"1#A": "a"},
        updated_by="admin@example.com",
        expected_version=1,
    )
    assert len(dynamo.gets()) == 1


def test_a_save_against_the_version_the_caller_read_succeeds() -> None:
    store, _, _ = build()
    seed(store)
    assert (
        store.write_profile(
            "custom",
            {"summary_prompt_templates": {"1#SUMMARY": "first edit"}},
            updated_by="admin@example.com",
            expected_version=1,
        )
        == 2
    )


def test_a_save_against_a_stale_version_is_refused() -> None:
    """Two admin sessions on the same page: the second must not win silently."""
    store, dynamo, _ = build()
    seed(store)
    store.write_profile(
        "custom",
        {"summary_prompt_templates": {"1#SUMMARY": "what the first admin saved"}},
        updated_by="first@example.com",
        expected_version=1,
    )

    with pytest.raises(ConcurrentModification) as raised:
        store.write_profile(
            "custom",
            {"summary_prompt_templates": {"1#SUMMARY": "what the second admin saved"}},
            updated_by="second@example.com",
            expected_version=1,
        )

    assert "first@example.com" in str(raised.value), "the message should name who saved"
    assert dynamo.body("Config#custom")["summary_prompt_templates"]["1#SUMMARY"] == (
        "what the first admin saved"
    )


def test_the_write_itself_carries_the_condition_not_only_the_pre_check() -> None:
    """A save landing between the read and the write must still be refused.

    The pre-check reads the stored version and compares it; on its own that
    leaves a window. The condition on the write closes it, so it is asserted on
    the request actually sent.
    """
    store, dynamo, _ = build()
    seed(store)
    dynamo.requests.clear()
    store.write_profile(
        "custom",
        {"summary_prompt_templates": {"1#SUMMARY": "an edit"}},
        updated_by="admin@example.com",
        expected_version=1,
    )
    conditional = [request for request in dynamo.puts() if request.get("ConditionExpression")]
    assert conditional, "the profile was saved with no condition at all"
    assert conditional[-1]["ConditionExpression"] == "#version = :expected"
    assert conditional[-1]["ExpressionAttributeValues"][":expected"] == {"N": "1"}


def test_a_first_save_is_conditional_on_the_record_being_absent() -> None:
    """Version 0 means "I read nothing", which must not overwrite a record."""
    store, dynamo, _ = build()
    store.write_default(DEFAULT_BODY)
    store.write_profile(
        "custom",
        {"summary_prompt_templates": {"1#SUMMARY": "the first edit"}},
        updated_by="admin@example.com",
        expected_version=0,
    )
    assert dynamo.puts()[-1]["ConditionExpression"] == "attribute_not_exists(#key)"


def test_the_pre_check_reads_consistently() -> None:
    """An eventually consistent read could report a version already superseded."""
    store, dynamo, _ = build()
    seed(store)
    dynamo.requests.clear()
    store.write_profile("custom", {}, updated_by="admin@example.com", expected_version=1)
    assert dynamo.gets()[0]["ConsistentRead"] is True


def test_a_save_may_opt_out_of_the_guard_by_offering_no_version() -> None:
    """For a caller with no version to offer, such as a one-off repair script."""
    store, dynamo, _ = build()
    seed(store)
    store.write_profile("custom", {}, updated_by="scripted", expected_version=None)
    assert dynamo.puts()[-1].get("ConditionExpression") is None


def test_the_baseline_keeps_the_time_it_was_first_seeded() -> None:
    """Otherwise `CreatedAt` on the baseline would mean "last deployed".

    It is rewritten on every deployment that changes the shipped content, so
    without carrying the value forward it would be indistinguishable from
    `UpdatedAt` and would tell an operator nothing.
    """
    store, dynamo, _ = build(timestamps=["2026-01-01T00:00:00Z", "2026-06-06T00:00:00Z"])
    store.write_default(DEFAULT_BODY)
    store.write_default(DEFAULT_BODY)
    metadata = dynamo.metadata("Config#default")
    assert metadata["CreatedAt"] == "2026-01-01T00:00:00Z"
    assert metadata["UpdatedAt"] == "2026-06-06T00:00:00Z"


def test_the_record_keeps_the_time_it_was_created() -> None:
    store, dynamo, _ = build(
        timestamps=["2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z", "2026-01-03T00:00:00Z"]
    )
    store.seed_profile("custom", {})
    store.write_profile("custom", {}, updated_by="admin@example.com", expected_version=1)
    metadata = dynamo.metadata("Config#custom")
    assert metadata["CreatedAt"] == "2026-01-01T00:00:00Z"
    assert metadata["UpdatedAt"] == "2026-01-02T00:00:00Z"


def test_converting_a_record_to_the_compressed_layout_leaves_nothing_behind() -> None:
    """A whole-item write, so stale top-level attributes do not accumulate."""
    store, dynamo, _ = build()
    dynamo.items["Config#custom"] = {
        "Configuration": {"S": "Config#custom"},
        "1#SUMMARY": {"S": "an inline prompt"},
        "*Information*": {"S": "guidance text"},
    }
    store.write_profile(
        "custom",
        {"summary_prompt_templates": {"1#SUMMARY": "now compressed"}},
        updated_by="admin@example.com",
        expected_version=0,
    )
    stored = dynamo.items["Config#custom"]
    assert "1#SUMMARY" not in stored
    assert "*Information*" not in stored
    assert dynamo.body("Config#custom") == {
        "summary_prompt_templates": {"1#SUMMARY": "now compressed"}
    }


def test_a_section_can_be_saved_over_an_inline_record_holding_a_number() -> None:
    """The conversion the inline layout rests on has to survive being written.

    A number read back from DynamoDB arrives as a `Decimal` and a string set as
    a `set`, and neither is a JSON type. A save of one section merges the rest
    of the body forward, so an inline record holding a numeric attribute could
    be read and then not written back -- and the failure would reach the
    administrator as "could not save", nowhere near the cause.
    """
    store, dynamo, _ = build()
    store.write_default(DEFAULT_BODY)
    dynamo.items["Config#custom"] = {
        "Configuration": {"S": "Config#custom"},
        "a_number": {"N": "42"},
        "a_fraction": {"N": "1.5"},
        "a_set": {"SS": ["b", "a"]},
        "a_list": {"L": [{"N": "7"}]},
        "a_map": {"M": {"inner": {"N": "3"}}},
    }
    store.write_section(
        "custom",
        "summary_prompt_templates",
        {"1#SUMMARY": "my prompt"},
        updated_by="admin@example.com",
        expected_version=0,
    )
    assert dynamo.body("Config#custom") == {
        "a_number": 42,
        "a_fraction": 1.5,
        "a_set": ["a", "b"],
        "a_list": [7],
        "a_map": {"inner": 3},
        "summary_prompt_templates": {"1#SUMMARY": "my prompt"},
    }


def test_a_body_that_is_not_json_is_refused_as_itself() -> None:
    """Otherwise the caller's message says only that the save did not work."""
    store, _, _ = build()
    with pytest.raises(ConfigurationError, match="JSON types"):
        store.write_default({"section": {"key": object()}})


def test_the_first_save_against_an_unversioned_record_is_still_conditional() -> None:
    """ "No version" and "no record" are not the same thing.

    Every record this package writes carries a `Version`. One without it is an
    inline record -- lifted in by a later store migration, or repaired by hand in
    the console -- and reads as version 0, the same number as an absent record.
    The write is conditioned on the record still being un-versioned rather than
    on its being absent, which both lets the save through and still refuses one
    that a concurrent save got in ahead of.
    """
    store, dynamo, _ = build()
    dynamo.items["Config#custom"] = {
        "Configuration": {"S": "Config#custom"},
        "1#SUMMARY": {"S": "an inline prompt"},
    }
    store.write_profile("custom", {}, updated_by="admin@example.com", expected_version=0)
    assert dynamo.puts()[-1]["ConditionExpression"] == "attribute_not_exists(#version)"

    # A second save offering the same version 0 is now refused: the record it
    # described has already been converted.
    with pytest.raises(ConcurrentModification):
        store.write_profile("custom", {}, updated_by="someone-else@example.com", expected_version=0)


# ── reserved names ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", ["__active", "default", "schema"])
def test_a_write_cannot_take_a_name_the_store_owns(name: str) -> None:
    """Otherwise a profile could be created that overwrites the pointer."""
    store, dynamo, _ = build()
    with pytest.raises(ReservedProfileName):
        store.write_profile(name, {}, updated_by="admin@example.com", expected_version=0)
    with pytest.raises(ReservedProfileName):
        store.seed_profile(name, {})
    with pytest.raises(ReservedProfileName):
        store.set_active_profile(name)
    assert not dynamo.puts(), "a refused name still reached DynamoDB"


# ── the merge ──────────────────────────────────────────────────────────────


def test_the_merge_recurses_into_nested_objects() -> None:
    assert deep_merge(
        {"a": {"keep": 1, "replace": 1}, "b": 1},
        {"a": {"replace": 2}, "c": 3},
    ) == {"a": {"keep": 1, "replace": 2}, "b": 1, "c": 3}


def test_the_merge_replaces_a_list_rather_than_appending_to_it() -> None:
    """An operator removing an entry from a list must see it removed."""
    assert deep_merge({"a": [1, 2, 3]}, {"a": [9]}) == {"a": [9]}


def test_the_merge_does_not_mutate_either_input() -> None:
    base = {"a": {"x": 1}}
    overlay = {"a": {"y": 2}}
    deep_merge(base, overlay)
    assert base == {"a": {"x": 1}}
    assert overlay == {"a": {"y": 2}}
