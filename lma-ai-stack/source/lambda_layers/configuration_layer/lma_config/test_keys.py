#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Tests for the configuration table's key layout.

The key space is the part of this design that cannot be changed later without a
data migration, and two of its properties are load-bearing rather than tidy:

* A profile may not be stored under a name the store owns. ``Config#__active``
  is the pointer naming the profile in force, and ``Config#default`` is the
  baseline CloudFormation rewrites; a profile created under either name would
  be a profile that overwrites one of them.
* Enumerating profiles is a ``begins_with(Configuration, "Config#")`` query, so
  anything that must not appear in that list either sits outside the prefix
  (the revision index) or is filtered out by name (the pointer). Both halves are
  asserted here, because the UI's profile list is built on them.

No AWS calls: every function under test is pure.
"""

from __future__ import annotations

import pytest

from lma_config import keys
from lma_config.errors import InvalidProfileName, ReservedProfileName


def test_the_baseline_and_the_pointer_have_the_keys_the_design_names() -> None:
    """Spelled out rather than derived, so a change to either is deliberate."""
    assert keys.DEFAULT_KEY == "Config#default"
    assert keys.ACTIVE_POINTER_KEY == "Config#__active"
    assert keys.SCHEMA_KEY == "Schema"


def test_the_revision_index_prefix_is_outside_the_profile_prefix() -> None:
    """A revision index must not come back from the profile enumeration.

    Profiles are listed with `begins_with(Configuration, "Config#")`. The
    revision index deliberately does not share that prefix, which is the whole
    reason it is spelled `ConfigRevIndex#` and not `Config#RevIndex#`.
    """
    assert not keys.REVISION_INDEX_PREFIX.startswith(keys.CONFIG_PREFIX)
    assert not keys.is_profile_key(f"{keys.REVISION_INDEX_PREFIX}production")


@pytest.mark.parametrize("name", ["custom", "production", "teams-heavy", "v1.2", "a_b", "A"])
def test_an_ordinary_name_is_accepted(name: str) -> None:
    assert keys.validate_profile_name(name) == name
    assert keys.profile_key(name) == f"Config#{name}"
    assert keys.is_profile_key(f"Config#{name}")


@pytest.mark.parametrize(
    "name",
    ["__active", "__ACTIVE", "default", "DEFAULT", "Default", "schema", "Schema", "__anything"],
)
def test_a_name_the_store_owns_is_refused(name: str) -> None:
    """The pointer, the baseline and the schema document are not profiles."""
    with pytest.raises(ReservedProfileName):
        keys.validate_profile_name(name)
    with pytest.raises(ReservedProfileName):
        keys.profile_key(name)


@pytest.mark.parametrize(
    "name",
    [
        "with space",
        "with/slash",
        "with#hash",
        "with:colon",
        "../escape",
        "with\\backslash",
        "with\nnewline",
        # A trailing newline specifically: Python's `$` matches just before one,
        # so this is the name a `match` would have let through and the
        # TypeScript half's `test` would have refused. The two halves are
        # supposed to accept exactly the same names, and the contract test
        # compares the patterns' text, which cannot see the difference.
        "custom\n",
        "héllo",
        "",
    ],
)
def test_a_name_outside_the_permitted_set_is_refused(name: str) -> None:
    """A name appears verbatim in the partition key and later in an S3 key.

    Restricting the set is what means a name can never introduce structure into
    either, so nothing downstream has to escape one.
    """
    with pytest.raises(InvalidProfileName):
        keys.validate_profile_name(name)


def test_a_name_longer_than_the_limit_is_refused() -> None:
    assert keys.validate_profile_name("a" * keys.MAX_PROFILE_NAME_LENGTH)
    with pytest.raises(InvalidProfileName):
        keys.validate_profile_name("a" * (keys.MAX_PROFILE_NAME_LENGTH + 1))


def test_a_name_that_is_not_a_string_is_refused() -> None:
    """The name reaches here from a GraphQL argument, so the type is checked."""
    for value in (None, 7, ["custom"], {"name": "custom"}):
        with pytest.raises(InvalidProfileName):
            keys.validate_profile_name(value)  # type: ignore[arg-type]


def test_a_key_whose_name_has_a_trailing_newline_is_not_a_profile() -> None:
    """The same difference, on the other side of the module."""
    assert not keys.is_profile_key("Config#custom\n")


def test_the_profile_enumeration_excludes_the_keys_the_store_owns() -> None:
    """What the UI's profile list would be, built from the prefix query."""
    stored = [
        "Config#default",
        "Config#__active",
        "Config#custom",
        "Config#production",
        "Schema",
        "ConfigRevIndex#custom",
    ]
    assert [key for key in stored if keys.is_profile_key(key)] == [
        "Config#custom",
        "Config#production",
    ]


def test_the_profile_name_is_recoverable_from_its_key() -> None:
    assert keys.profile_name_from_key("Config#custom") == "custom"
    with pytest.raises(ValueError):
        keys.profile_name_from_key("Schema")
