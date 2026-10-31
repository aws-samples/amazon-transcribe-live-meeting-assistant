#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Tests for the configuration revision history.

The guarantees asserted here, most damaging first:

* **No profile body is replaced unless it is recorded.** A save first records
  the state it replaces when that state is not already a revision -- a profile
  saved before history began, or a revision whose body did not land -- and is
  refused, with nothing written, if it cannot.
* **Revision numbers are never reused.** They are allocated in the profile's own
  conditional write, and carried by every write, including one made by a store
  that keeps no history.
* **Restore is forward-only.** The restored body becomes a new revision, and the
  one it replaced stays retained.
* **A save that changes nothing records nothing**, so repeated deployments do
  not push real history out of the retention window.
* **Retention keeps what it promises**: the newest revisions, the published one,
  and anything labelled.

DynamoDB and S3 are fakes over dicts. The DynamoDB fake implements exactly the
condition and update expressions the store sends, so a test fails if the store
starts sending one nobody has thought about.
"""

from __future__ import annotations

import gzip
import io
import json
from typing import Any, Callable, Dict, List, Optional

import pytest
from botocore.exceptions import ClientError

from lma_config import codec
from lma_config.errors import (
    ConcurrentModification,
    InvalidProfileName,
    ReservedProfileName,
    RevisionHistoryUnavailable,
    RevisionNotFound,
)
from lma_config.keys import is_profile_key
from lma_config.revisions import (
    SOURCE_BACKFILL,
    SOURCE_DEPLOY,
    SOURCE_RECOVERED,
    SOURCE_RESTORE,
    SOURCE_SAVE,
    diff_bodies,
    revision_index_key,
    revision_object_key,
)
from lma_config.store import ConfigurationStore

TABLE = "lma-configuration"
BUCKET = "lma-configuration-revisions"
PROFILE = "custom"
PROFILE_KEY = "Config#custom"
INDEX_KEY = "ConfigRevIndex#custom"
SECTION = "nova_sonic_config"
OTHER_SECTION = "asr_engine"


def _refused(operation: str) -> ClientError:
    return ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException", "Message": "refused"}}, operation
    )


class FakeDynamo:
    """The DynamoDB calls the store makes, over a dict of AttributeValue items."""

    APPEND = (
        "SET #revisions = list_append(if_not_exists(#revisions, :empty), :new), "
        "#updated = :now ADD #seq :one"
    )
    REWRITE = "SET #revisions = :entries, #updated = :now, #seq = :next"
    COUNTERS = "SET #latest = :revision, #published = :revision"

    def __init__(self) -> None:
        self.items: Dict[str, Dict[str, Any]] = {}
        self.requests: List[Dict[str, Any]] = []
        #: Called with the request before an update is applied, so a test can
        #: land a concurrent change between a read and the write that follows.
        self.before_update: Optional[Callable[[Dict[str, Any]], None]] = None

    def get_item(self, **request: Any) -> Dict[str, Any]:
        self.requests.append({"op": "get_item", **request})
        item = self.items.get(request["Key"][codec.KEY_ATTRIBUTE]["S"])
        # A copy, as DynamoDB hands back: a caller mutating what it read must
        # not change what is stored.
        return {"Item": {name: dict(value) for name, value in item.items()}} if item else {}

    def put_item(self, **request: Any) -> Dict[str, Any]:
        self.requests.append({"op": "put_item", **request})
        item = request["Item"]
        key = item[codec.KEY_ATTRIBUTE]["S"]
        stored = self.items.get(key)
        condition = request.get("ConditionExpression")
        if condition == "attribute_not_exists(#key)" and stored is not None:
            raise _refused("PutItem")
        if condition == "attribute_not_exists(#version)" and "Version" in (stored or {}):
            raise _refused("PutItem")
        if condition == "#version = :expected":
            expected = request["ExpressionAttributeValues"][":expected"]["N"]
            if (stored or {}).get("Version", {}).get("N") != expected:
                raise _refused("PutItem")
        assert condition in (
            None,
            "attribute_not_exists(#key)",
            "attribute_not_exists(#version)",
            "#version = :expected",
        ), condition
        self.items[key] = item
        return {}

    def update_item(self, **request: Any) -> Dict[str, Any]:
        self.requests.append({"op": "update_item", **request})
        if self.before_update is not None:
            hook, self.before_update = self.before_update, None
            hook(request)
        key = request["Key"][codec.KEY_ATTRIBUTE]["S"]
        values = request["ExpressionAttributeValues"]
        expression = request["UpdateExpression"]
        stored = self.items.get(key)
        if expression == self.APPEND:
            item = stored or {codec.KEY_ATTRIBUTE: {"S": key}}
            item["Revisions"] = {"L": item.get("Revisions", {"L": []})["L"] + values[":new"]["L"]}
            item["UpdatedAt"] = values[":now"]
            item["IndexSeq"] = {"N": str(int(item.get("IndexSeq", {"N": "0"})["N"]) + 1)}
            self.items[key] = item
            return {}
        if expression == self.REWRITE:
            condition = request["ConditionExpression"]
            held = (stored or {}).get("IndexSeq")
            if condition == "attribute_not_exists(#seq)" and held is not None:
                raise _refused("UpdateItem")
            if condition == "#seq = :seq" and (held or {}).get("N") != values[":seq"]["N"]:
                raise _refused("UpdateItem")
            item = stored or {codec.KEY_ATTRIBUTE: {"S": key}}
            item["Revisions"] = values[":entries"]
            item["UpdatedAt"] = values[":now"]
            item["IndexSeq"] = values[":next"]
            self.items[key] = item
            return {}
        if expression == self.COUNTERS:
            condition = request["ConditionExpression"]
            if stored is None or "PublishedRevision" in stored:
                raise _refused("UpdateItem")
            if "#version = :expected" in condition:
                if stored.get("Version", {}).get("N") != values[":expected"]["N"]:
                    raise _refused("UpdateItem")
            stored["LatestRevision"] = values[":revision"]
            stored["PublishedRevision"] = values[":revision"]
            return {}
        raise AssertionError(f"unexpected update expression: {expression}")

    # ── helpers ────────────────────────────────────────────────────────────

    def metadata(self, key: str) -> Dict[str, Any]:
        return codec.split_item(codec.unmarshal_item(self.items[key]))[0]

    def body(self, key: str) -> Dict[str, Any]:
        return codec.decode_body(self.items[key][codec.BODY_ATTRIBUTE]["B"])

    def index_numbers(self) -> List[int]:
        item = codec.unmarshal_item(self.items.get(INDEX_KEY, {}))
        return [int(entry["Revision"]) for entry in item.get("Revisions", [])]


class FakeS3:
    """``put_object`` / ``get_object`` / ``delete_object`` over a dict."""

    def __init__(self) -> None:
        self.objects: Dict[str, bytes] = {}
        self.fail_puts: int = 0
        self.deny_gets: bool = False
        self.puts: List[str] = []

    def put_object(self, **request: Any) -> Dict[str, Any]:
        assert request["Bucket"] == BUCKET
        if self.fail_puts:
            self.fail_puts -= 1
            raise ClientError({"Error": {"Code": "InternalError", "Message": "down"}}, "PutObject")
        self.puts.append(request["Key"])
        self.objects[request["Key"]] = request["Body"]
        return {}

    def get_object(self, **request: Any) -> Dict[str, Any]:
        if self.deny_gets:
            raise ClientError({"Error": {"Code": "AccessDenied", "Message": "no"}}, "GetObject")
        if request["Key"] not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey", "Message": "absent"}}, "GetObject")
        return {"Body": io.BytesIO(self.objects[request["Key"]])}

    def delete_object(self, **request: Any) -> Dict[str, Any]:
        self.objects.pop(request["Key"], None)
        return {}

    def envelope(self, revision: int) -> Dict[str, Any]:
        raw = self.objects[revision_object_key(PROFILE, revision)]
        return json.loads(gzip.decompress(raw).decode("utf-8"))


class Timestamps:
    def __init__(self) -> None:
        self.count = 0

    def __call__(self) -> str:
        self.count += 1
        return f"2026-10-31T00:00:{self.count:02d}Z"


def build(
    *, retain: int = 20, dynamo: Optional[FakeDynamo] = None, s3: Optional[FakeS3] = None
) -> tuple[ConfigurationStore, FakeDynamo, FakeS3]:
    the_dynamo = dynamo or FakeDynamo()
    the_s3 = s3 or FakeS3()
    store = ConfigurationStore(
        TABLE,
        client=the_dynamo,
        now=Timestamps(),
        revision_bucket=BUCKET,
        s3_client=the_s3,
        retained_revisions=retain,
    )
    return store, the_dynamo, the_s3


def save(store: ConfigurationStore, values: Dict[str, Any], **kwargs: Any) -> int:
    kwargs.setdefault("updated_by", "admin@example.com")
    kwargs.setdefault("expected_version", None)
    return store.write_section(PROFILE, SECTION, values, **kwargs)


# ── recording ───────────────────────────────────────────────────────────────


def test_a_save_that_changes_the_body_records_who_when_which_sections_and_why() -> None:
    store, dynamo, s3 = build()
    store.seed_profile(PROFILE, {})

    save(store, {"voiceId": "matthew"}, comment="  deeper voice  ")

    listing = store.list_revisions(PROFILE)
    newest = listing.entries[0]
    assert newest.revision == 2
    assert newest.created_by == "admin@example.com"
    assert newest.created_at == dynamo.metadata(PROFILE_KEY)["UpdatedAt"]
    assert newest.sections == (SECTION,)
    assert newest.comment == "deeper voice"
    assert newest.source == SOURCE_SAVE
    assert newest.version == 2
    assert s3.envelope(2)["Body"] == {SECTION: {"voiceId": "matthew"}}
    assert listing.published_revision == listing.latest_revision == 2


def test_only_the_sections_a_save_changed_are_named() -> None:
    store, _, _ = build()
    store.seed_profile(PROFILE, {OTHER_SECTION: {"streamingEngineMicrovm": True}})

    save(store, {"voiceId": "matthew"})

    assert store.list_revisions(PROFILE).entries[0].sections == (SECTION,)


def test_a_save_that_changes_nothing_records_no_revision() -> None:
    store, dynamo, s3 = build()
    store.seed_profile(PROFILE, {})
    save(store, {"voiceId": "matthew"})
    puts_before = list(s3.puts)

    save(store, {"voiceId": "matthew"})

    assert s3.puts == puts_before
    assert dynamo.index_numbers() == [1, 2]
    assert dynamo.metadata(PROFILE_KEY)["PublishedRevision"] == 2


def test_a_value_changing_type_is_a_change() -> None:
    store, dynamo, _ = build()
    store.seed_profile(PROFILE, {SECTION: {"groupMeetingMode": 1}})

    save(store, {"groupMeetingMode": True})

    assert dynamo.index_numbers() == [1, 2]


def test_a_refused_save_records_nothing() -> None:
    store, dynamo, s3 = build()
    store.seed_profile(PROFILE, {})
    save(store, {"voiceId": "matthew"})
    puts_before = list(s3.puts)

    with pytest.raises(ConcurrentModification):
        save(store, {"voiceId": "joanna"}, expected_version=1)

    assert s3.puts == puts_before
    assert dynamo.metadata(PROFILE_KEY)["LatestRevision"] == 2


def test_the_revision_number_is_written_in_the_profiles_own_conditional_write() -> None:
    store, dynamo, _ = build()
    store.seed_profile(PROFILE, {})

    save(store, {"voiceId": "matthew"}, expected_version=1)

    write = [r for r in dynamo.requests if r["op"] == "put_item"][-1]
    assert write["ConditionExpression"] == "#version = :expected"
    assert write["Item"]["LatestRevision"] == {"N": "2"}
    assert write["Item"]["PublishedRevision"] == {"N": "2"}


def test_a_created_profile_starts_at_revision_one() -> None:
    store, dynamo, s3 = build()

    assert store.seed_profile(PROFILE, {SECTION: {"voiceId": "matthew"}}, origin="created")

    entry = store.list_revisions(PROFILE).entries[0]
    assert (entry.revision, entry.source, entry.created_by) == (1, SOURCE_DEPLOY, "cloudformation")
    assert entry.sections == (SECTION,)
    assert s3.envelope(1)["Body"] == {SECTION: {"voiceId": "matthew"}}
    assert dynamo.metadata(PROFILE_KEY)["ProfileOrigin"] == "created"


def test_a_seed_that_finds_the_profile_already_there_writes_no_revision() -> None:
    store, _, s3 = build()
    store.seed_profile(PROFILE, {})
    save(store, {"voiceId": "matthew"})
    puts_before = list(s3.puts)

    assert not store.seed_profile(PROFILE, {"anything": {}})

    assert s3.puts == puts_before
    assert s3.envelope(1)["Body"] == {}


def test_a_seeded_section_is_recorded_as_a_deployment() -> None:
    store, _, _ = build()
    store.seed_profile(PROFILE, {})

    assert store.seed_section(PROFILE, SECTION, {"voiceId": "matthew"})

    entry = store.list_revisions(PROFILE).entries[0]
    assert (entry.revision, entry.source, entry.sections) == (2, SOURCE_DEPLOY, (SECTION,))


# ── the upgrade, and recovering a revision that did not land ────────────────


def _profile_saved_before_history(dynamo: FakeDynamo) -> None:
    """A profile as a pre-release build of the store, or a writer keeping no history, left it."""
    legacy = ConfigurationStore(TABLE, client=dynamo, now=lambda: "2026-01-01T00:00:00Z")
    legacy.seed_profile(PROFILE, {})
    legacy.write_section(
        PROFILE,
        SECTION,
        {"voiceId": "matthew"},
        updated_by="earlier@example.com",
        expected_version=None,
    )
    assert "LatestRevision" not in dynamo.metadata(PROFILE_KEY)


def test_a_profile_saved_before_history_began_is_recorded_before_it_is_replaced() -> None:
    dynamo = FakeDynamo()
    _profile_saved_before_history(dynamo)
    store, _, s3 = build(dynamo=dynamo)

    save(store, {"voiceId": "joanna"})

    backfill, new = sorted(store.list_revisions(PROFILE).entries, key=lambda e: e.revision)
    assert (backfill.revision, backfill.source) == (1, SOURCE_BACKFILL)
    assert backfill.created_by == "earlier@example.com"
    assert backfill.created_at == "2026-01-01T00:00:00Z"
    assert s3.envelope(1)["Body"] == {SECTION: {"voiceId": "matthew"}}
    assert (new.revision, new.source) == (2, SOURCE_SAVE)
    assert s3.envelope(2)["Body"] == {SECTION: {"voiceId": "joanna"}}


def test_the_seeder_starts_history_without_counting_as_a_save() -> None:
    dynamo = FakeDynamo()
    _profile_saved_before_history(dynamo)
    before = dynamo.metadata(PROFILE_KEY)
    store, _, s3 = build(dynamo=dynamo)

    assert store.ensure_revision_history(PROFILE) == "started"

    after = dynamo.metadata(PROFILE_KEY)
    for unchanged in ("Version", "UpdatedBy", "UpdatedAt"):
        assert after[unchanged] == before[unchanged]
    assert (after["LatestRevision"], after["PublishedRevision"]) == (1, 1)
    assert s3.envelope(1)["Body"] == {SECTION: {"voiceId": "matthew"}}
    assert store.ensure_revision_history(PROFILE) == "present"


def test_starting_history_yields_to_a_save_that_lands_meanwhile() -> None:
    dynamo = FakeDynamo()
    _profile_saved_before_history(dynamo)
    store, _, _ = build(dynamo=dynamo)
    rival, _, _ = build(dynamo=dynamo, s3=FakeS3())

    def an_admin_saves(_request: Dict[str, Any]) -> None:
        save(rival, {"voiceId": "joanna"})

    dynamo.before_update = an_admin_saves
    assert store.ensure_revision_history(PROFILE) == "saved-concurrently"
    assert dynamo.metadata(PROFILE_KEY)["PublishedRevision"] == 2


def test_a_save_is_refused_when_the_state_it_replaces_cannot_be_recorded() -> None:
    dynamo = FakeDynamo()
    _profile_saved_before_history(dynamo)
    s3 = FakeS3()
    s3.fail_puts = 1
    store, _, _ = build(dynamo=dynamo, s3=s3)
    before = dynamo.metadata(PROFILE_KEY)

    with pytest.raises(RevisionHistoryUnavailable, match="the save was not made"):
        save(store, {"voiceId": "joanna"})

    # Not replaced: the record has been given its revision number, and nothing else.
    after = dynamo.metadata(PROFILE_KEY)
    assert dynamo.body(PROFILE_KEY) == {SECTION: {"voiceId": "matthew"}}
    for unchanged in ("Version", "UpdatedBy", "UpdatedAt"):
        assert after[unchanged] == before[unchanged]
    assert after["PublishedRevision"] == 1

    # The next save records it from the record, then saves.
    save(store, {"voiceId": "joanna"})
    assert s3.envelope(1)["Body"] == {SECTION: {"voiceId": "matthew"}}
    assert (s3.envelope(1)["Source"], s3.envelope(1)["CreatedBy"]) == (
        SOURCE_RECOVERED,
        "earlier@example.com",
    )
    assert s3.envelope(2)["Body"] == {SECTION: {"voiceId": "joanna"}}


def test_two_first_saves_racing_cannot_store_two_bodies_under_one_number() -> None:
    dynamo = FakeDynamo()
    _profile_saved_before_history(dynamo)
    s3 = FakeS3()
    slow, _, _ = build(dynamo=dynamo, s3=s3)
    fast, _, _ = build(dynamo=dynamo, s3=s3)

    def the_other_admin_saves_first(_request: Dict[str, Any]) -> None:
        save(fast, {"voiceId": "joanna"}, updated_by="fast@example.com")

    # Both read the record before either wrote; the fast one finishes in
    # between the slow one's read and its first write.
    dynamo.before_update = the_other_admin_saves_first
    with pytest.raises(ConcurrentModification):
        save(slow, {"voiceId": "ruth"}, expected_version=2, updated_by="slow@example.com")

    assert s3.envelope(1)["Body"] == {SECTION: {"voiceId": "matthew"}}
    assert s3.envelope(2)["Body"] == {SECTION: {"voiceId": "joanna"}}
    assert s3.envelope(2)["CreatedBy"] == "fast@example.com"
    assert sorted(dynamo.index_numbers()) == [1, 2]
    assert revision_object_key(PROFILE, 3) not in s3.objects


def test_numbers_continue_above_the_index_when_the_counters_were_lost() -> None:
    store, dynamo, s3 = build()
    store.seed_profile(PROFILE, {})
    save(store, {"voiceId": "matthew"})
    # A writer that keeps no history rewrites the record without its
    # counters -- code that predates them -- while the index survives.
    del dynamo.items[PROFILE_KEY]["LatestRevision"]
    del dynamo.items[PROFILE_KEY]["PublishedRevision"]

    save(store, {"voiceId": "joanna"})

    assert sorted(dynamo.index_numbers()) == [1, 2, 3, 4]
    assert s3.envelope(2)["Body"] == {SECTION: {"voiceId": "matthew"}}
    assert s3.envelope(3)["Source"] == SOURCE_BACKFILL
    assert s3.envelope(4)["Body"] == {SECTION: {"voiceId": "joanna"}}


def test_a_profile_recreated_after_deletion_continues_its_numbering() -> None:
    store, dynamo, s3 = build()
    store.seed_profile(PROFILE, {})
    save(store, {"voiceId": "matthew"})
    del dynamo.items[PROFILE_KEY]

    assert store.seed_profile(PROFILE, {})

    assert dynamo.metadata(PROFILE_KEY)["PublishedRevision"] == 3
    assert s3.envelope(2)["Body"] == {SECTION: {"voiceId": "matthew"}}
    assert s3.envelope(3)["Body"] == {}


def test_the_seeder_puts_back_a_current_revision_whose_object_is_missing() -> None:
    store, _, s3 = build()
    store.seed_profile(PROFILE, {})
    save(store, {"voiceId": "matthew"})
    s3.objects.clear()  # a bucket replaced while the table was not

    assert store.ensure_revision_history(PROFILE) == "recovered"

    assert s3.envelope(2)["Body"] == {SECTION: {"voiceId": "matthew"}}
    assert store.ensure_revision_history(PROFILE) == "present"


def test_a_revision_whose_body_did_not_land_is_recovered_before_it_is_replaced() -> None:
    store, dynamo, s3 = build()
    store.seed_profile(PROFILE, {})
    s3.fail_puts = 1
    save(store, {"voiceId": "matthew"}, comment="lost with the object")  # saved; r2 not stored

    assert dynamo.body(PROFILE_KEY) == {SECTION: {"voiceId": "matthew"}}
    listed = store.list_revisions(PROFILE).entries[0]
    assert (listed.revision, listed.source) == (2, SOURCE_RECOVERED)
    assert store.get_revision(PROFILE, 2).body == {SECTION: {"voiceId": "matthew"}}

    save(store, {"voiceId": "joanna"}, updated_by="second@example.com")

    assert s3.envelope(2)["Body"] == {SECTION: {"voiceId": "matthew"}}
    recovered = store.get_revision(PROFILE, 2).entry
    assert (recovered.source, recovered.created_by) == (SOURCE_RECOVERED, "admin@example.com")
    assert s3.envelope(3)["Body"] == {SECTION: {"voiceId": "joanna"}}


def test_a_stored_revision_missing_only_its_index_entry_keeps_its_own_metadata() -> None:
    store, dynamo, s3 = build()
    store.seed_profile(PROFILE, {})
    save(store, {"voiceId": "matthew"}, comment="kept in the object")
    # The index entry for r2 is lost, its object is not.
    store._revisions.rewrite(  # pylint: disable=protected-access
        PROFILE, lambda entries: [e for e in entries if e.revision != 2]
    )

    save(store, {"voiceId": "joanna"})

    restored_entry = store.get_revision(PROFILE, 2).entry
    assert (restored_entry.source, restored_entry.comment) == (SOURCE_SAVE, "kept in the object")
    assert sorted(dynamo.index_numbers()) == [1, 2, 3]


def test_numbers_are_not_reused_after_a_write_that_keeps_no_history() -> None:
    store, dynamo, s3 = build()
    store.seed_profile(PROFILE, {})
    save(store, {"voiceId": "matthew"})
    no_history = ConfigurationStore(TABLE, client=dynamo, now=lambda: "2026-10-31T01:00:00Z")

    no_history.write_section(
        PROFILE, SECTION, {"voiceId": "joanna"}, updated_by="x@example.com", expected_version=None
    )

    metadata = dynamo.metadata(PROFILE_KEY)
    assert metadata["LatestRevision"] == 2
    assert "PublishedRevision" not in metadata
    save(store, {"voiceId": "ruth"})
    assert s3.envelope(2)["Body"] == {SECTION: {"voiceId": "matthew"}}
    assert s3.envelope(3)["Body"] == {SECTION: {"voiceId": "joanna"}}
    assert s3.envelope(3)["Source"] == SOURCE_BACKFILL
    assert s3.envelope(4)["Body"] == {SECTION: {"voiceId": "ruth"}}


def test_a_store_without_a_bucket_keeps_no_history(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CONFIGURATION_BUCKET_NAME", raising=False)
    store = ConfigurationStore(TABLE, client=FakeDynamo())

    assert not store.keeps_history
    with pytest.raises(RevisionHistoryUnavailable):
        store.list_revisions(PROFILE)


def test_the_bucket_comes_from_one_environment_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONFIGURATION_BUCKET_NAME", BUCKET)

    assert ConfigurationStore(TABLE, client=FakeDynamo(), s3_client=FakeS3()).keeps_history


# ── restore ─────────────────────────────────────────────────────────────────


def test_restore_records_the_old_body_as_a_new_revision() -> None:
    store, dynamo, s3 = build()
    store.seed_profile(PROFILE, {})
    save(store, {"voiceId": "matthew"})
    save(store, {"voiceId": "joanna"})

    outcome = store.restore_revision(
        PROFILE, 2, updated_by="restorer@example.com", expected_version=3, comment="back"
    )

    assert (outcome.version, outcome.revision) == (4, 4)
    assert dynamo.body(PROFILE_KEY) == {SECTION: {"voiceId": "matthew"}}
    entry = store.list_revisions(PROFILE).entries[0]
    assert (entry.source, entry.restored_from, entry.comment) == (SOURCE_RESTORE, 2, "back")
    assert entry.created_by == "restorer@example.com"
    # The state it replaced is still there to go back to.
    assert store.get_revision(PROFILE, 3).body == {SECTION: {"voiceId": "joanna"}}
    assert s3.envelope(4)["RestoredFrom"] == 2


def test_restoring_the_body_the_profile_already_holds_records_nothing() -> None:
    store, dynamo, _ = build()
    store.seed_profile(PROFILE, {})
    save(store, {"voiceId": "matthew"})

    outcome = store.restore_revision(PROFILE, 2, updated_by="a@example.com", expected_version=2)

    assert outcome.revision is None
    assert dynamo.index_numbers() == [1, 2]


def test_restore_is_refused_against_a_profile_that_moved_on() -> None:
    store, dynamo, _ = build()
    store.seed_profile(PROFILE, {})
    save(store, {"voiceId": "matthew"})
    save(store, {"voiceId": "joanna"})

    with pytest.raises(ConcurrentModification):
        store.restore_revision(PROFILE, 2, updated_by="a@example.com", expected_version=2)

    assert dynamo.body(PROFILE_KEY) == {SECTION: {"voiceId": "joanna"}}


def test_restoring_a_revision_that_is_not_retained_is_refused() -> None:
    store, _, _ = build()
    store.seed_profile(PROFILE, {})

    with pytest.raises(RevisionNotFound):
        store.restore_revision(PROFILE, 9, updated_by="a@example.com", expected_version=1)


# ── retention ───────────────────────────────────────────────────────────────


def test_pruning_keeps_the_newest_revisions_and_every_labelled_one() -> None:
    store, dynamo, s3 = build(retain=3)
    store.seed_profile(PROFILE, {})
    save(store, {"voiceId": "v2"})
    assert store.label_revision(PROFILE, 2, "  known good ")
    for voice in ("v3", "v4", "v5", "v6"):
        save(store, {"voiceId": voice})

    assert sorted(dynamo.index_numbers()) == [2, 4, 5, 6]
    assert revision_object_key(PROFILE, 1) not in s3.objects
    assert revision_object_key(PROFILE, 3) not in s3.objects
    assert store.get_revision(PROFILE, 2).entry.label == "known good"
    with pytest.raises(RevisionNotFound):
        store.get_revision(PROFILE, 3)


def test_the_published_revision_is_kept_however_old() -> None:
    store, _, _ = build()
    store.seed_profile(PROFILE, {})
    save(store, {"voiceId": "v2"})
    save(store, {"voiceId": "v3"})
    log = store._revisions  # pylint: disable=protected-access
    log.retain = 1

    assert log.prune(PROFILE, protected=(1,)) == [2]

    assert {e.revision for e in store.list_revisions(PROFILE).entries} == {1, 3}


def test_clearing_a_label_makes_the_revision_prunable_again() -> None:
    store, dynamo, _ = build(retain=2)
    store.seed_profile(PROFILE, {})
    store.label_revision(PROFILE, 1, "first")
    save(store, {"voiceId": "v2"})
    save(store, {"voiceId": "v3"})
    assert sorted(dynamo.index_numbers()) == [1, 2, 3]

    store.label_revision(PROFILE, 1, None)
    save(store, {"voiceId": "v4"})

    assert sorted(dynamo.index_numbers()) == [3, 4]


def test_labelling_a_revision_that_is_not_retained_reports_it() -> None:
    store, _, _ = build()
    store.seed_profile(PROFILE, {})

    assert not store.label_revision(PROFILE, 7, "nope")


def test_a_rewrite_retries_when_an_append_lands_between_its_read_and_its_write() -> None:
    store, dynamo, _ = build()
    store.seed_profile(PROFILE, {})
    log = store._revisions  # pylint: disable=protected-access

    def a_save_appends(_request: Dict[str, Any]) -> None:
        save(store, {"voiceId": "landed meanwhile"})

    dynamo.before_update = a_save_appends
    assert log.set_label(PROFILE, 1, "first")

    assert sorted(dynamo.index_numbers()) == [1, 2]
    assert store.get_revision(PROFILE, 1).entry.label == "first"


# ── reading and comparing ───────────────────────────────────────────────────


def test_the_diff_names_every_changed_value_by_its_path() -> None:
    before = {
        "summary_prompt_templates": {"1#SUMMARY": "old", "2#ACTIONS": "same"},
        SECTION: {"voiceId": "matthew", "groupMeetingMode": 1},
    }
    after = {
        "summary_prompt_templates": {"2#ACTIONS": "same", "3#TOPICS": "new"},
        SECTION: {"voiceId": "joanna", "groupMeetingMode": True},
        OTHER_SECTION: {"streamingEngineMicrovm": True},
    }

    assert diff_bodies(before, after) == [
        {"Path": [OTHER_SECTION], "Change": "added", "After": {"streamingEngineMicrovm": True}},
        {"Path": [SECTION, "groupMeetingMode"], "Change": "changed", "Before": 1, "After": True},
        {"Path": [SECTION, "voiceId"], "Change": "changed", "Before": "matthew", "After": "joanna"},
        {"Path": ["summary_prompt_templates", "1#SUMMARY"], "Change": "removed", "Before": "old"},
        {"Path": ["summary_prompt_templates", "3#TOPICS"], "Change": "added", "After": "new"},
    ]


def test_two_revisions_can_be_compared() -> None:
    store, _, _ = build()
    store.seed_profile(PROFILE, {})
    save(store, {"voiceId": "matthew"})

    assert store.diff_revisions(PROFILE, 1, 2) == [
        {"Path": [SECTION], "Change": "added", "After": {"voiceId": "matthew"}}
    ]


def test_a_denied_read_is_not_reported_as_a_missing_revision() -> None:
    store, _, s3 = build()
    store.seed_profile(PROFILE, {})
    s3.deny_gets = True

    with pytest.raises(Exception, match="s3:ListBucket") as raised:
        store.get_revision(PROFILE, 1)
    assert not isinstance(raised.value, RevisionNotFound)


def test_the_index_is_one_entry_per_revision_preferring_the_saves_own() -> None:
    store, dynamo, _ = build()
    store.seed_profile(PROFILE, {})
    log = store._revisions  # pylint: disable=protected-access
    head = store.read_profile(PROFILE, consistent_read=True)
    log.append(PROFILE, [store._recovered_entry(head, 1)])  # pylint: disable=protected-access

    entries = store.list_revisions(PROFILE).entries

    assert [(e.revision, e.source) for e in entries] == [(1, SOURCE_DEPLOY)]
    assert dynamo.index_numbers() == [1, 1]


# ── the key space ───────────────────────────────────────────────────────────


def test_the_revision_index_is_never_enumerated_as_a_profile() -> None:
    assert revision_index_key(PROFILE) == INDEX_KEY
    assert not is_profile_key(INDEX_KEY)


@pytest.mark.parametrize("name", ["../custom", "a/b", "", "x" * 65])
def test_a_revision_key_cannot_leave_its_prefix(name: str) -> None:
    with pytest.raises(InvalidProfileName):
        revision_object_key(name, 1)


def test_the_baseline_has_no_revision_history() -> None:
    store, _, _ = build()

    with pytest.raises(ReservedProfileName):
        store.list_revisions("default")


def test_revision_objects_are_numbered_to_six_places() -> None:
    assert revision_object_key(PROFILE, 7) == "config_revisions/custom/000007.json.gz"
