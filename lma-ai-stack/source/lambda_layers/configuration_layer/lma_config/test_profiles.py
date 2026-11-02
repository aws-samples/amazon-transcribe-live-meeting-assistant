# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Named profiles: full copies, created, listed, activated and deleted.

The baseline and Managed profiles are read-only; Custom profiles are full copies
of the configuration, merged over the baseline when read so that a setting a
later release adds still has a value.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_store import TABLE, FakeDynamo  # noqa: E402

from lma_config import codec  # noqa: E402
from lma_config.errors import (  # noqa: E402
    InvalidConfiguration,
    ProfileExists,
    ProfileInUse,
    ProfileNotFound,
    ProfileReadOnly,
    ReservedProfileName,
)
from lma_config.store import FULL_COPY, ConfigurationStore  # noqa: E402

BASELINE = {
    "language_models": {
        "summaryModelId": "amazon.nova-lite-v1:0",
        "meetingAssistModelId": "amazon.nova-micro-v1:0",
    },
    "retention": {"transcriptDeletionAfterSummary": "off"},
}


class ProfileDynamo(FakeDynamo):
    """The fake, with the scan and the conditional delete the profile operations use."""

    def scan(self, **request: Any) -> Dict[str, Any]:
        prefix = request["ExpressionAttributeValues"][":prefix"]["S"]
        items = []
        for key, item in self.items.items():
            if key.startswith(prefix):
                items.append({k: v for k, v in item.items() if k != codec.BODY_ATTRIBUTE})
        return {"Items": items}

    def delete_item(self, **request: Any) -> Dict[str, Any]:
        key = request["Key"][codec.KEY_ATTRIBUTE]["S"]
        if request.get("ConditionExpression") == "#version = :expected":
            expected = request["ExpressionAttributeValues"][":expected"]["N"]
            if self.items.get(key, {}).get("Version", {}).get("N") != expected:
                raise self._refused()
        self.items.pop(key, None)
        return {}


@pytest.fixture
def store():
    dynamo = ProfileDynamo()
    the_store = ConfigurationStore(TABLE, client=dynamo, now=lambda: "2026-11-02T00:00:00Z")
    the_store.write_default(BASELINE, description="shipped")
    the_store.seed_profile(
        "custom", {"language_models": {"summaryModelId": "amazon.nova-pro-v1:0"}}
    )
    the_store.set_active_profile("custom")
    the_store.dynamo = dynamo
    return the_store


def _body(store, name):
    return store.dynamo.body(f"Config#{name}")


def _metadata(store, name):
    return store.dynamo.metadata(f"Config#{name}")


# ── creating ────────────────────────────────────────────────────────────────


def test_a_new_profile_is_a_full_copy_of_the_baseline(store):
    store.create_profile("trial", updated_by="admin@example.com")
    assert _body(store, "trial") == BASELINE
    assert _metadata(store, "trial")["ProfileFormat"] == FULL_COPY
    assert _metadata(store, "trial")["ProfileOrigin"] == "copied-from-default"
    assert _metadata(store, "trial")["UpdatedBy"] == "admin@example.com"


def test_a_copy_keeps_the_source_profiles_settings_and_fills_the_rest(store):
    store.create_profile("trial", updated_by="a", copy_from="custom", description="A trial")
    body = _body(store, "trial")
    assert body["language_models"] == {
        "summaryModelId": "amazon.nova-pro-v1:0",
        "meetingAssistModelId": "amazon.nova-micro-v1:0",
    }
    assert body["retention"] == BASELINE["retention"]
    assert _metadata(store, "trial")["Description"] == "A trial"


def test_an_imported_configuration_is_laid_over_the_baseline(store):
    store.create_profile(
        "imported",
        updated_by="a",
        body={"retention": {"transcriptDeletionAfterSummary": "immediately"}},
    )
    body = _body(store, "imported")
    assert body["retention"] == {"transcriptDeletionAfterSummary": "immediately"}
    assert body["language_models"] == BASELINE["language_models"]
    assert _metadata(store, "imported")["ProfileOrigin"] == "imported"


def test_an_invalid_import_creates_nothing(store):
    with pytest.raises(InvalidConfiguration):
        store.create_profile(
            "bad", updated_by="a", body={"retention": {"transcriptDeletionAfterSummary": "soon"}}
        )
    assert "Config#bad" not in store.dynamo.items


def test_a_name_in_use_is_refused(store):
    with pytest.raises(ProfileExists):
        store.create_profile("custom", updated_by="a")


def test_copying_a_profile_that_is_not_there_is_refused(store):
    with pytest.raises(ProfileNotFound):
        store.create_profile("trial", updated_by="a", copy_from="missing")


def test_the_baselines_name_cannot_be_taken(store):
    with pytest.raises(ReservedProfileName):
        store.create_profile("default", updated_by="a")


# ── listing and activating ──────────────────────────────────────────────────


def test_the_list_starts_with_the_baseline_and_marks_the_active_profile(store):
    store.create_profile("trial", updated_by="a")
    profiles = store.list_profiles()
    assert [p.name for p in profiles] == ["default", "custom", "trial"]
    by_name = {p.name: p for p in profiles}
    assert by_name["default"].managed is True
    assert by_name["custom"].active is True
    assert by_name["trial"].full_copy is True
    assert by_name["custom"].full_copy is False  # a delta until the seeder converts it


def test_the_list_leaves_out_the_pointer(store):
    assert "__active" not in [p.name for p in store.list_profiles()]


def test_activating_a_profile_changes_what_readers_resolve(store):
    store.create_profile("trial", updated_by="a")
    store.activate_profile("trial")
    store.invalidate()
    assert store.get_section("language_models").values["summaryModelId"] == "amazon.nova-lite-v1:0"
    assert store.active_profile_name() == "trial"


def test_the_baseline_itself_can_be_activated(store):
    store.activate_profile("default")
    store.invalidate()
    assert store.active_profile_name() is None
    assert store.get_section("language_models").values == BASELINE["language_models"]
    assert [p.name for p in store.list_profiles() if p.active] == ["default"]


def test_a_profile_that_is_not_there_cannot_be_activated(store):
    with pytest.raises(ProfileNotFound):
        store.activate_profile("missing")


# ── deleting ────────────────────────────────────────────────────────────────


def test_a_custom_profile_that_is_not_active_is_deleted(store):
    store.create_profile("trial", updated_by="a")
    store.delete_profile("trial")
    assert "Config#trial" not in store.dynamo.items


def test_the_active_profile_is_not_deleted(store):
    with pytest.raises(ProfileInUse):
        store.delete_profile("custom")


def test_a_profile_that_is_not_there_is_not_deleted(store):
    with pytest.raises(ProfileNotFound):
        store.delete_profile("missing")


def test_the_baseline_is_not_deleted(store):
    with pytest.raises(ReservedProfileName):
        store.delete_profile("default")


# ── Managed profiles are read-only ──────────────────────────────────────────


def _managed(store, name="healthcare"):
    store.seed_profile(name, BASELINE)
    item = store.dynamo.items[f"Config#{name}"]
    item["Managed"] = codec.marshal_value(True)
    return name


def test_a_managed_profile_is_refused_a_save(store):
    name = _managed(store)
    with pytest.raises(ProfileReadOnly):
        store.write_section(
            name,
            "retention",
            {"transcriptDeletionAfterSummary": "immediately"},
            updated_by="a",
            expected_version=None,
        )
    with pytest.raises(ProfileReadOnly):
        store.write_profile(name, BASELINE, updated_by="a", expected_version=None)


def test_a_managed_profile_is_not_deleted(store):
    name = _managed(store)
    with pytest.raises(ProfileReadOnly):
        store.delete_profile(name)


def test_a_copy_of_a_managed_profile_is_custom(store):
    name = _managed(store)
    store.create_profile("mine", updated_by="a", copy_from=name)
    assert not _metadata(store, "mine").get("Managed")
    store.write_section(
        "mine",
        "retention",
        {"transcriptDeletionAfterSummary": "immediately"},
        updated_by="a",
        expected_version=None,
    )


# ── full copies ─────────────────────────────────────────────────────────────


def test_a_section_saved_as_a_delta_into_a_full_copy_is_completed(store):
    store.create_profile("trial", updated_by="a")
    store.write_section(
        "trial",
        "language_models",
        {"summaryModelId": "us.amazon.nova-premier-v1:0"},
        updated_by="a",
        expected_version=None,
    )
    assert _body(store, "trial")["language_models"] == {
        "summaryModelId": "us.amazon.nova-premier-v1:0",
        "meetingAssistModelId": "amazon.nova-micro-v1:0",
    }


def test_a_delta_profile_is_converted_keeping_its_settings(store):
    store.invalidate()
    before = store.get_section("language_models").values
    assert store.convert_to_full_copy("custom") is True
    assert _metadata(store, "custom")["ProfileFormat"] == FULL_COPY
    assert _body(store, "custom")["language_models"]["summaryModelId"] == "amazon.nova-pro-v1:0"
    assert "retention" in _body(store, "custom")
    store.invalidate()
    assert store.get_section("language_models").values == before


def test_a_profile_is_converted_once(store):
    assert store.convert_to_full_copy("custom") is True
    assert store.convert_to_full_copy("custom") is False


def test_a_managed_profile_is_not_converted(store):
    assert store.convert_to_full_copy(_managed(store)) is False


def test_a_full_copy_keeps_its_format_and_origin_across_saves(store):
    store.create_profile("trial", updated_by="a", description="kept")
    store.write_section(
        "trial",
        "language_models",
        {"summaryModelId": "us.amazon.nova-premier-v1:0"},
        updated_by="b",
        expected_version=None,
    )
    metadata = _metadata(store, "trial")
    assert metadata["ProfileFormat"] == FULL_COPY
    assert metadata["ProfileOrigin"] == "copied-from-default"
    assert metadata["Description"] == "kept"


def test_a_delta_revision_restored_into_a_full_copy_is_completed():
    from test_revisions import BUCKET, FakeS3
    from test_revisions import FakeDynamo as HistoryDynamo

    store = ConfigurationStore(
        TABLE, client=HistoryDynamo(), revision_bucket=BUCKET, s3_client=FakeS3()
    )
    store.write_default(BASELINE)
    store.seed_profile("custom", {"language_models": {"summaryModelId": "amazon.nova-pro-v1:0"}})
    assert store.convert_to_full_copy("custom")
    head = store.read_profile("custom", consistent_read=True)
    store.restore_revision("custom", 1, updated_by="a", expected_version=head.version)
    body = store.read_profile("custom", consistent_read=True).body
    assert body["language_models"] == {
        "summaryModelId": "amazon.nova-pro-v1:0",
        "meetingAssistModelId": "amazon.nova-micro-v1:0",
    }
    assert "retention" in body
