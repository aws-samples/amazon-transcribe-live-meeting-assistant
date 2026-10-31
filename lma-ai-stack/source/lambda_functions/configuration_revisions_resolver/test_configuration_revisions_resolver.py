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
