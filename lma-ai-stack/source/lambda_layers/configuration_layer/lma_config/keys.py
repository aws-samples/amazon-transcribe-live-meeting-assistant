#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""The key space of the configuration table.

One table, one HASH key named ``Configuration``, no sort key and no secondary
indexes. Everything the store holds is distinguished by a prefix on that single
key:

====================== =======================================================
``Config#default``     The deployment baseline. CloudFormation writes it on
                       create and rewrites it whenever the content a release
                       ships changes, so an operator edit does not belong here.
``Config#<name>``      A profile holding the operator's edits as a delta over
                       the baseline.
``Config#__active``    The pointer naming the profile in force.
``Schema``             The form schema the UI renders. Reserved here; written
                       by the schema-driven editor.
``ConfigRevIndex#...`` The revision metadata index. Reserved here; written by
                       the revision work.
====================== =======================================================

The ``ConfigRevIndex#`` prefix is deliberately *not* under ``Config#``: profile
enumeration is a ``begins_with(Configuration, "Config#")`` query, and a revision
index sharing that prefix would come back from it looking like a profile.

``Config#__active`` does share the profile prefix, so :func:`is_profile_key`
filters it out by name rather than by prefix, and :func:`validate_profile_name`
refuses the reserved names outright: a profile that could be created under the
name ``__active`` would be a profile that overwrites the pointer.
"""

from __future__ import annotations

import re

CONFIG_PREFIX = "Config#"
REVISION_INDEX_PREFIX = "ConfigRevIndex#"
SCHEMA_KEY = "Schema"

DEFAULT_PROFILE_NAME = "default"
ACTIVE_POINTER_NAME = "__active"

DEFAULT_KEY = f"{CONFIG_PREFIX}{DEFAULT_PROFILE_NAME}"
ACTIVE_POINTER_KEY = f"{CONFIG_PREFIX}{ACTIVE_POINTER_NAME}"

# Names the store owns. Compared case-insensitively: DynamoDB keys are
# case-sensitive, so `Config#__ACTIVE` would not literally overwrite the
# pointer, but a profile named that way is a profile named after the pointer and
# nothing good follows from allowing it.
RESERVED_PROFILE_NAMES = frozenset({DEFAULT_PROFILE_NAME, ACTIVE_POINTER_NAME, "schema"})

# Profile names appear verbatim inside the partition key, and from the revision
# work onwards inside an S3 object key as well. Restricting them to this set
# means a name can never introduce structure into either, so no escaping is
# needed anywhere a name is used.
PROFILE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

MAX_PROFILE_NAME_LENGTH = 64


def validate_profile_name(name: str) -> str:
    """Return ``name`` if a profile may be stored under it, else raise.

    Raises:
        InvalidProfileName: the name is empty, too long, or holds a character
            outside ``[A-Za-z0-9._-]``.
        ReservedProfileName: the name is one of the keys the store owns, or
            starts with ``__``, which is how the store marks its own keys.
    """
    # Imported here rather than at module scope so this module stays free of
    # package-internal imports and can be read on its own.
    from .errors import InvalidProfileName, ReservedProfileName

    if not isinstance(name, str) or not name:
        raise InvalidProfileName("a configuration profile name is required")
    if len(name) > MAX_PROFILE_NAME_LENGTH:
        raise InvalidProfileName(
            f"a configuration profile name may be at most "
            f"{MAX_PROFILE_NAME_LENGTH} characters; got {len(name)}"
        )
    # `fullmatch`, not `match`: in Python `$` also matches just before a
    # trailing newline, so "custom\n" would pass a `match` here and fail the
    # TypeScript half's `test`, and the two are supposed to accept exactly the
    # same names.
    if not PROFILE_NAME_PATTERN.fullmatch(name):
        raise InvalidProfileName(
            "a configuration profile name may hold only letters, digits, '.', "
            f"'_' and '-'; got {name!r}"
        )
    lowered = name.lower()
    if lowered.startswith("__") or lowered in RESERVED_PROFILE_NAMES:
        raise ReservedProfileName(
            f"{name!r} is reserved by the configuration store; "
            f"reserved names are {sorted(RESERVED_PROFILE_NAMES)} and anything "
            "starting with '__'"
        )
    return name


def profile_key(name: str) -> str:
    """The partition key of a user profile, after validating its name."""
    return f"{CONFIG_PREFIX}{validate_profile_name(name)}"


def is_profile_key(key: str) -> bool:
    """Whether ``key`` names a user profile.

    False for the baseline, for the active pointer, for the schema document and
    for a revision index, so an enumeration built on the ``Config#`` prefix does
    not present any of them as a profile.
    """
    if not key.startswith(CONFIG_PREFIX):
        return False
    name = key[len(CONFIG_PREFIX) :]
    if not PROFILE_NAME_PATTERN.fullmatch(name):  # see validate_profile_name
        return False
    lowered = name.lower()
    return not lowered.startswith("__") and lowered not in RESERVED_PROFILE_NAMES


def profile_name_from_key(key: str) -> str:
    """The profile name carried by a ``Config#<name>`` key."""
    if not key.startswith(CONFIG_PREFIX):
        raise ValueError(f"{key!r} is not a configuration key")
    return key[len(CONFIG_PREFIX) :]
