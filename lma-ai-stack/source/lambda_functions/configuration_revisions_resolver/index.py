#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""AppSync resolver for the configuration history.

Every save into the configuration store records a revision (see
``docs/configuration-store.md``). This function serves the five fields that
read and act on that history, dispatching on the field name AppSync reports:

``Query.listConfigurationRevisions(Profile)``
    The retained revisions, newest first: who saved each, when, which sections
    it changed, its comment and label. Also the profile's current version,
    which is what a restore sends back.

``Query.getConfigurationRevision(Profile, Revision)``
    One revision's metadata and its whole body, as a JSON string.

``Query.diffConfigurationRevisions(Profile, FromRevision, ToRevision)``
    Every value that differs between two revisions, as a JSON list of
    ``{"Path", "Change", "Before", "After"}``.

``Mutation.restoreConfigurationRevision(input)``
    Save an earlier revision's body as the profile's current one. Forward-only:
    the restore is itself recorded as a new revision, so what it replaced can be
    restored in turn. Conditional on the version the caller read.

``Mutation.labelConfigurationRevision(input)``
    Label a revision, or clear its label. A labelled revision is kept however
    many revisions follow it.

All five are for the **Admin** group: on the AppSync field, and re-checked here.
The profile defaults to ``custom``, the one profile every configuration page
saves into.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, Mapping

# pylint: disable=import-error
from lma_config import (  # from the configuration layer
    ConcurrentModification,
    ConfigurationStore,
    RevisionEntry,
    RevisionNotFound,
    validate_profile_name,
)

# pylint: enable=import-error

# Stdlib logging rather than aws_lambda_powertools: this function carries only
# the configuration layer, the same choice the other configuration resolvers
# make.
LOGGER = logging.getLogger()
LOGGER.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

ADMIN_GROUP = os.environ.get("ADMIN_GROUP", "Admin")
DEFAULT_PROFILE = "custom"

LIST_FIELD = "listConfigurationRevisions"
GET_FIELD = "getConfigurationRevision"
DIFF_FIELD = "diffConfigurationRevisions"
RESTORE_FIELD = "restoreConfigurationRevision"
LABEL_FIELD = "labelConfigurationRevision"
FIELDS = (LIST_FIELD, GET_FIELD, DIFF_FIELD, RESTORE_FIELD, LABEL_FIELD)

# Built once per container. Every read here is a fresh, consistent one: the
# history is read through `list_revisions` and `get_revision`, which never use
# the store's section cache.
CONFIG_STORE = ConfigurationStore()


class ForbiddenError(Exception):
    """The caller is not a member of the group these fields require."""


def _caller(event: Mapping[str, Any]) -> Dict[str, Any]:
    """Who is asking, from the AppSync ``identity`` block only.

    ``cognito:groups`` arrives as a list from the user-pool authorizer and as a
    comma-joined string from some token shapes, so both are read. The actor is
    the same identity the configuration pages record as ``UpdatedBy``: the
    email address, or the username where the token carries none.
    """
    identity = event.get("identity") or {}
    claims = identity.get("claims") or {}
    groups = claims.get("cognito:groups") or identity.get("groups") or []
    if isinstance(groups, str):
        groups = [group.strip() for group in groups.split(",") if group.strip()]
    elif not isinstance(groups, (list, tuple)):
        groups = []
    return {
        "actor": str(
            claims.get("email")
            or claims.get("cognito:username")
            or identity.get("username")
            or claims.get("sub")
            or "unknown"
        ),
        # Logged in place of the address; see the other configuration resolvers.
        "subject": str(claims.get("sub") or identity.get("sub") or ""),
        "is_admin": ADMIN_GROUP in groups,
    }


def _require_admin(caller: Mapping[str, Any]) -> None:
    if not caller["is_admin"]:
        LOGGER.info(
            "refused a configuration history request to %s", caller["subject"] or "(no subject)"
        )
        raise ForbiddenError(
            f"Unauthorized: only members of the {ADMIN_GROUP} group can read or restore "
            "the configuration history"
        )


def _field(event: Mapping[str, Any]) -> str:
    field = ((event.get("info") or {}).get("fieldName") or "").strip()
    if field not in FIELDS:
        raise ValueError(f"this resolver does not serve {field or '(nothing)'}")
    return field


def _profile(arguments: Mapping[str, Any]) -> str:
    return validate_profile_name(str(arguments.get("Profile") or DEFAULT_PROFILE))


def _revision_number(arguments: Mapping[str, Any], name: str) -> int:
    raw = arguments.get(name)
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        raise ValueError(f"{name} must be a positive revision number")
    return raw


def _entry_shape(entry: RevisionEntry, published: Any) -> Dict[str, Any]:
    """One revision in the ``ConfigurationRevision`` GraphQL shape."""
    return {
        "Revision": entry.revision,
        "CreatedAt": entry.created_at or None,
        "CreatedBy": entry.created_by or None,
        "Sections": list(entry.sections) if entry.sections is not None else None,
        "Comment": entry.comment,
        "Source": entry.source,
        "RestoredFrom": entry.restored_from,
        "Label": entry.label,
        "Version": entry.version,
        "SizeBytes": entry.size_bytes,
        "Published": entry.revision == published,
    }


def list_revisions(arguments: Mapping[str, Any]) -> Dict[str, Any]:
    profile = _profile(arguments)
    listing = CONFIG_STORE.list_revisions(profile)
    return {
        "Profile": profile,
        "Version": listing.version,
        "LatestRevision": listing.latest_revision,
        "PublishedRevision": listing.published_revision,
        "Revisions": [_entry_shape(entry, listing.published_revision) for entry in listing.entries],
    }


def get_revision(arguments: Mapping[str, Any]) -> Dict[str, Any]:
    profile = _profile(arguments)
    record = CONFIG_STORE.get_revision(profile, _revision_number(arguments, "Revision"))
    head = CONFIG_STORE.read_profile(profile, consistent_read=True)
    return {
        **_entry_shape(record.entry, head.published_revision),
        "Profile": profile,
        "Body": json.dumps(record.body, sort_keys=True),
    }


def diff_revisions(arguments: Mapping[str, Any]) -> Dict[str, Any]:
    profile = _profile(arguments)
    before = _revision_number(arguments, "FromRevision")
    after = _revision_number(arguments, "ToRevision")
    changes = CONFIG_STORE.diff_revisions(profile, before, after)
    return {
        "Profile": profile,
        "FromRevision": before,
        "ToRevision": after,
        "Changes": json.dumps(changes, sort_keys=True),
    }


def restore_revision(input_data: Mapping[str, Any], caller: Mapping[str, Any]) -> Dict[str, Any]:
    profile = _profile(input_data)
    revision = _revision_number(input_data, "Revision")
    expected = input_data.get("ExpectedVersion")
    if isinstance(expected, bool) or not isinstance(expected, int):
        # Required: a restore replaces every section of the profile, so a
        # restore made from a stale read would discard whatever was saved since.
        raise ValueError(
            "ExpectedVersion is required: send the Version listConfigurationRevisions returned"
        )
    outcome = CONFIG_STORE.restore_revision(
        profile,
        revision,
        updated_by=caller["actor"],
        expected_version=expected,
        comment=input_data.get("Comment"),
    )
    LOGGER.info(
        "restored revision %d of %s as %s (version %d)",
        revision,
        profile,
        f"revision {outcome.revision}" if outcome.revision else "no new revision",
        outcome.version,
    )
    return {
        "Success": True,
        "Profile": profile,
        "Version": outcome.version,
        "Revision": outcome.revision,
        "Unchanged": outcome.revision is None,
    }


def label_revision(input_data: Mapping[str, Any]) -> Dict[str, Any]:
    profile = _profile(input_data)
    revision = _revision_number(input_data, "Revision")
    label = input_data.get("Label")
    if not CONFIG_STORE.label_revision(profile, revision, label):
        raise RevisionNotFound(f"revision {revision} of {profile!r} is not retained")
    return {"Success": True, "Profile": profile, "Revision": revision}


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """AppSync entry point."""
    field = _field(event)
    caller = _caller(event)
    # Checked before the table is read, and outside the handling below so the
    # refusal reaches the client as itself.
    _require_admin(caller)
    arguments = event.get("arguments") or {}
    try:
        if field == LIST_FIELD:
            return list_revisions(arguments)
        if field == GET_FIELD:
            return get_revision(arguments)
        if field == DIFF_FIELD:
            return diff_revisions(arguments)
        if field == RESTORE_FIELD:
            return restore_revision(arguments.get("input") or {}, caller)
        return label_revision(arguments.get("input") or {})
    except ConcurrentModification as error:
        # The prefix the configuration pages match on to tell the admin to
        # reload. The message names whoever saved the profile, which belongs in
        # the answer to the admin, not in CloudWatch.
        LOGGER.info("refused a restore against a superseded profile")
        raise Exception(f"Conflict: {error}") from error  # pylint: disable=broad-exception-raised
    except RevisionNotFound as error:
        raise Exception(f"Not found: {error}") from error  # pylint: disable=broad-exception-raised
