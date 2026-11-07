# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Unit tests for the configuration history resolver (no AWS calls).

What is asserted:

* **Every field is for the Admin group**, re-checked here and not only on the
  AppSync field, and the refusal happens before anything is read.
* **A restore is attributed to the verified caller** and is conditional on the
  version they read; a stale one is refused with the prefix the configuration
  pages match on to tell the admin to reload.
* **The shapes match the schema**: bodies and diffs as JSON strings, revisions
  with who, when, which sections and whether they are the current state.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

HERE = Path(__file__).resolve().parent
SOURCE = HERE.parents[1]
LAYER = SOURCE / "lambda_layers" / "configuration_layer"
if str(LAYER) not in sys.path:
    sys.path.insert(0, str(LAYER))

os.environ.setdefault("CONFIGURATION_TABLE_NAME", "lma-configuration")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

from lma_config.errors import UnknownConfigurationSection  # noqa: E402
from lma_config.store import ConfigurationStore  # noqa: E402
from lma_config.test_revisions import BUCKET, TABLE, FakeDynamo, FakeS3  # noqa: E402

_SPEC = importlib.util.spec_from_file_location(
    "configuration_revisions_resolver_index", HERE / "index.py"
)
index = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(index)

ADMIN = {"claims": {"cognito:groups": ["Admin"], "email": "admin@example.com", "sub": "s-1"}}
USER = {"claims": {"cognito:groups": ["User"], "email": "user@example.com", "sub": "s-2"}}
SECTION = "nova_sonic_config"


@pytest.fixture(name="store")
def _store(monkeypatch: pytest.MonkeyPatch) -> ConfigurationStore:
    store = ConfigurationStore(
        TABLE, client=FakeDynamo(), revision_bucket=BUCKET, s3_client=FakeS3()
    )
    store.seed_profile("custom", {})
    store.write_section(
        "custom",
        SECTION,
        {"voiceId": "matthew"},
        updated_by="first@example.com",
        expected_version=None,
        comment="first edit",
    )
    monkeypatch.setattr(index, "CONFIG_STORE", store)
    return store


def call(field: str, identity: Optional[Dict[str, Any]] = None, **arguments: Any) -> Any:
    return index.handler(
        {"info": {"fieldName": field}, "identity": identity or ADMIN, "arguments": arguments},
        None,
    )


@pytest.mark.parametrize("field", index.FIELDS)
def test_every_field_is_refused_to_a_caller_outside_the_admin_group(
    field: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Untouchable:
        def __getattr__(self, name: str) -> Any:
            raise AssertionError(f"the store was used ({name}) before the caller was checked")

    monkeypatch.setattr(index, "CONFIG_STORE", Untouchable())

    with pytest.raises(index.ForbiddenError):
        call(field, USER, Profile="custom", Revision=1, input={"Revision": 1})


def test_an_unknown_field_is_refused() -> None:
    with pytest.raises(ValueError):
        call("listCalls")


def test_the_list_names_who_when_which_sections_and_the_current_revision(store) -> None:
    result = call("listConfigurationRevisions")

    assert (result["Profile"], result["Version"], result["PublishedRevision"]) == ("custom", 2, 2)
    newest, first = result["Revisions"]
    assert newest["Revision"] == 2
    assert newest["CreatedBy"] == "first@example.com"
    assert newest["Sections"] == [SECTION]
    assert newest["Comment"] == "first edit"
    assert newest["Published"] is True
    assert first["Published"] is False


def test_a_revision_is_returned_with_its_body_as_json(store) -> None:
    result = call("getConfigurationRevision", Revision=2)

    assert json.loads(result["Body"]) == {SECTION: {"voiceId": "matthew"}}
    assert result["Published"] is True


def test_a_diff_is_returned_as_a_json_list(store) -> None:
    result = call("diffConfigurationRevisions", FromRevision=1, ToRevision=2)

    assert json.loads(result["Changes"]) == [
        {"Path": [SECTION], "Change": "added", "After": {"voiceId": "matthew"}}
    ]


def test_a_restore_is_attributed_to_the_verified_caller(store) -> None:
    result = call(
        "restoreConfigurationRevision",
        input={"Revision": 1, "ExpectedVersion": 2, "Comment": "undo", "CreatedBy": "forged"},
    )

    assert (result["Success"], result["Version"], result["Revision"]) == (True, 3, 3)
    assert result["Unchanged"] is False
    entry = store.list_revisions("custom").entries[0]
    assert (entry.created_by, entry.restored_from, entry.comment) == (
        "admin@example.com",
        1,
        "undo",
    )


def test_a_restore_needs_the_version_the_caller_read(store) -> None:
    with pytest.raises(ValueError, match="ExpectedVersion"):
        call("restoreConfigurationRevision", input={"Revision": 1})


def test_a_restore_against_a_superseded_profile_is_reported_as_a_conflict(store) -> None:
    with pytest.raises(Exception, match="^Conflict: "):
        call("restoreConfigurationRevision", input={"Revision": 1, "ExpectedVersion": 1})


def test_a_revision_that_is_not_retained_is_reported_as_not_found(store) -> None:
    with pytest.raises(Exception, match="^Not found: "):
        call("getConfigurationRevision", Revision=40)


def test_a_label_is_stored_and_an_unknown_revision_is_not_found(store) -> None:
    assert call("labelConfigurationRevision", input={"Revision": 1, "Label": "baseline"})["Success"]
    assert store.get_revision("custom", 1).entry.label == "baseline"

    with pytest.raises(Exception, match="^Not found: "):
        call("labelConfigurationRevision", input={"Revision": 40, "Label": "x"})


@pytest.mark.parametrize("value", [0, -1, "2", True, None])
def test_a_revision_number_must_be_a_positive_integer(store, value: Any) -> None:
    with pytest.raises(ValueError, match="positive revision number"):
        call("getConfigurationRevision", Revision=value)


def test_a_profile_name_is_validated_before_it_reaches_a_key(store) -> None:
    with pytest.raises(Exception, match="profile name"):
        call("listConfigurationRevisions", Profile="../custom")


# ── the configuration editor's read and save ────────────────────────────────


def test_the_editor_read_serves_every_section_schema_with_its_tier(store) -> None:
    store.write_default({SECTION: {"voiceId": "tiffany"}, "unregistered_leftover": {"x": 1}})

    result = call("getConfiguration")

    sections = {entry["Name"]: entry for entry in json.loads(result["Sections"])}
    assert set(sections) == {
        "summary_prompt_templates",
        "nova_sonic_config",
        "chat_buttons",
        "asr_engine",
        "call_pricing",
        "language_models",
        "transcription",
        "recording",
        "meeting_assist",
        "meeting_processing",
        "virtual_participant",
        "retention",
        "meeting_consent",
        "meeting_agenda",
    }
    assert sections[SECTION]["CacheTier"] == "immediate"
    assert sections["language_models"]["CacheTier"] == "standard"
    assert sections[SECTION]["Schema"]["properties"]["voiceId"]["type"] == "string"
    # Only registered sections: the editor renders nothing it cannot validate.
    assert json.loads(result["Default"]) == {SECTION: {"voiceId": "tiffany"}}
    assert json.loads(result["Custom"]) == {SECTION: {"voiceId": "matthew"}}
    assert (result["Profile"], result["Exists"], result["Version"]) == ("custom", True, 2)
    assert result["UpdatedBy"] == "first@example.com"
    assert result["PublishedRevision"] == 2
    presets = {preset["Id"]: preset for preset in json.loads(result["Presets"])}
    assert presets["regulated-consent-required"]["Sections"]["meeting_consent"]["mode"] == "merge"


def test_the_editor_save_writes_every_section_it_names_as_one_revision(store) -> None:
    pricing = {"regions": {"default": {"transcribe/streaming": {"seconds": 0.0002}}}}
    result = call(
        "updateConfiguration",
        input={
            "Sections": json.dumps(
                {"asr_engine": {"streamingEngineMicrovm": True}, "call_pricing": pricing}
            ),
            "ExpectedVersion": 2,
            "Comment": "price and engine",
            "UpdatedBy": "forged",
        },
    )

    assert (result["Success"], result["Version"], result["Revision"], result["Unchanged"]) == (
        True,
        3,
        3,
        False,
    )
    body = store.read_profile("custom").body
    assert body[SECTION] == {"voiceId": "matthew"}, "a section the save did not name was changed"
    assert body["asr_engine"] == {"streamingEngineMicrovm": True}
    entry = store.list_revisions("custom").entries[0]
    assert (entry.created_by, entry.comment) == ("admin@example.com", "price and engine")
    assert sorted(entry.sections) == ["asr_engine", "call_pricing"]


def test_an_editor_save_that_changes_nothing_records_nothing(store) -> None:
    result = call(
        "updateConfiguration",
        input={"Sections": {SECTION: {"voiceId": "matthew"}}, "ExpectedVersion": 2},
    )
    assert result["Unchanged"] is True
    assert result["Revision"] is None


def test_an_editor_save_that_does_not_match_the_schema_is_refused_as_invalid(store) -> None:
    with pytest.raises(Exception, match="^Invalid: ") as raised:
        call(
            "updateConfiguration",
            input={
                "Sections": {SECTION: {"promptMode": "sometimes"}, "asr_engine": {"x": True}},
                "ExpectedVersion": 2,
            },
        )
    message = str(raised.value)
    assert "nova_sonic_config › promptMode" in message
    assert "asr_engine › x: is not a setting this section has" in message
    assert store.read_profile("custom").version == 2, "a refused save wrote something"


def test_an_editor_save_naming_an_unknown_section_is_refused_as_invalid(store) -> None:
    with pytest.raises(Exception, match="^Invalid: "):
        call("updateConfiguration", input={"Sections": {"bogus": {}}, "ExpectedVersion": 2})


def test_an_editor_save_against_a_superseded_profile_is_a_conflict(store) -> None:
    with pytest.raises(Exception, match="^Conflict: "):
        call("updateConfiguration", input={"Sections": {SECTION: {}}, "ExpectedVersion": 1})


@pytest.mark.parametrize("expected", [None, "2", True])
def test_an_editor_save_needs_the_version_the_editor_loaded(store, expected: Any) -> None:
    with pytest.raises(ValueError, match="ExpectedVersion"):
        call("updateConfiguration", input={"Sections": {SECTION: {}}, "ExpectedVersion": expected})


@pytest.mark.parametrize("sections", ["{}", "[]", "not json", None])
def test_an_editor_save_must_name_at_least_one_section(store, sections: Any) -> None:
    with pytest.raises(ValueError):
        call("updateConfiguration", input={"Sections": sections, "ExpectedVersion": 2})


def test_a_section_with_no_schema_is_not_reported_as_invalid_input(
    store, monkeypatch: pytest.MonkeyPatch
) -> None:
    def missing() -> Dict[str, Any]:
        raise UnknownConfigurationSection("no schema shipped")

    monkeypatch.setattr(index, "section_schemas", missing)
    with pytest.raises(UnknownConfigurationSection):
        call("getConfiguration")


def test_the_editor_does_not_create_a_profile(store) -> None:
    with pytest.raises(ValueError, match="no configuration profile"):
        call(
            "updateConfiguration",
            input={"Profile": "elsewhere", "Sections": {SECTION: {}}, "ExpectedVersion": 0},
        )
    assert not store.read_profile("elsewhere").exists


# ── named profiles ──────────────────────────────────────────────────────────


class ProfileDynamo(FakeDynamo):
    """The history fake, with the scan and the delete the profile fields use."""

    def scan(self, **request: Any) -> Dict[str, Any]:
        prefix = request["ExpressionAttributeValues"][":prefix"]["S"]
        return {
            "Items": [
                {k: v for k, v in item.items() if k != "Body"}
                for key, item in self.items.items()
                if key.startswith(prefix)
            ]
        }

    def delete_item(self, **request: Any) -> Dict[str, Any]:
        self.items.pop(request["Key"]["Configuration"]["S"], None)
        return {}


@pytest.fixture(name="profiles")
def _profiles(monkeypatch: pytest.MonkeyPatch) -> ConfigurationStore:
    store = ConfigurationStore(
        TABLE, client=ProfileDynamo(), revision_bucket=BUCKET, s3_client=FakeS3()
    )
    store.write_default({SECTION: {"voiceId": "tiffany", "endpointingSensitivity": "MEDIUM"}})
    store.seed_profile("custom", {SECTION: {"voiceId": "matthew"}})
    store.set_active_profile("custom")
    monkeypatch.setattr(index, "CONFIG_STORE", store)
    return store


def test_the_profiles_are_listed_with_the_baseline_first(profiles) -> None:
    call("createConfigurationProfile", input={"Name": "trial", "Description": "A trial"})
    listed = call("listConfigurationProfiles")["Profiles"]
    assert [p["Name"] for p in listed] == ["default", "custom", "trial"]
    assert listed[0]["Managed"] is True
    assert listed[1]["Active"] is True
    assert listed[2]["Description"] == "A trial"
    assert listed[2]["FullCopy"] is True


def test_a_copy_is_attributed_to_the_verified_caller(profiles) -> None:
    call("createConfigurationProfile", input={"Name": "trial", "CopyFrom": "custom"})
    head = profiles.read_profile("trial")
    assert head.updated_by == "admin@example.com"
    assert head.body[SECTION] == {"voiceId": "matthew", "endpointingSensitivity": "MEDIUM"}


def test_an_import_is_laid_over_the_baseline(profiles) -> None:
    call(
        "createConfigurationProfile",
        input={"Name": "imported", "Body": json.dumps({SECTION: {"voiceId": "amy"}})},
    )
    assert profiles.read_profile("imported").body[SECTION]["endpointingSensitivity"] == "MEDIUM"


def test_an_import_naming_an_unknown_section_is_invalid(profiles) -> None:
    with pytest.raises(Exception, match="^Invalid: "):
        call(
            "createConfigurationProfile",
            input={"Name": "imported", "Body": json.dumps({"no_such_section": {}})},
        )


def test_a_name_in_use_is_refused(profiles) -> None:
    with pytest.raises(Exception, match="^Refused: "):
        call("createConfigurationProfile", input={"Name": "custom"})


def test_the_active_profile_is_refused_deletion(profiles) -> None:
    with pytest.raises(Exception, match="^Refused: "):
        call("deleteConfigurationProfile", input={"Name": "custom"})


def test_a_profile_is_activated_and_another_deleted(profiles) -> None:
    call("createConfigurationProfile", input={"Name": "trial"})
    call("activateConfigurationProfile", input={"Name": "trial"})
    assert profiles.active_profile_name(consistent_read=True) == "trial"
    call("deleteConfigurationProfile", input={"Name": "custom"})
    assert not profiles.read_profile("custom").exists


def test_the_baseline_can_be_read_and_activated_but_not_saved(profiles) -> None:
    document = call("getConfiguration", Profile="default")
    assert document["Managed"] is True
    assert json.loads(document["Custom"]) == {}
    call("activateConfigurationProfile", input={"Name": "default"})
    assert call("getConfiguration", Profile="default")["Active"] is True
    with pytest.raises(Exception, match="reserved by the configuration store"):
        call(
            "updateConfiguration",
            input={
                "Profile": "default",
                "ExpectedVersion": 1,
                "Sections": json.dumps({SECTION: {"voiceId": "amy"}}),
            },
        )


def test_a_managed_profile_is_refused_a_save(profiles) -> None:
    profiles.seed_profile("healthcare", {SECTION: {"voiceId": "tiffany"}})
    item = profiles._client.items["Config#healthcare"]  # pylint: disable=protected-access
    item["Managed"] = {"BOOL": True}
    with pytest.raises(Exception, match="^Refused: "):
        call(
            "updateConfiguration",
            input={
                "Profile": "healthcare",
                "ExpectedVersion": 1,
                "Sections": json.dumps({SECTION: {"voiceId": "amy"}}),
            },
        )


def test_activating_a_profile_that_is_not_there_is_not_found(profiles) -> None:
    with pytest.raises(Exception, match="^Not found: "):
        call("activateConfigurationProfile", input={"Name": "missing"})
