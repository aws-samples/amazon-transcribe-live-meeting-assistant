#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""AppSync resolver for the configuration editor and the configuration history.

Serves the two fields the schema-driven configuration editor is built on, and
the five that read and act on the history every save records (see
``docs/configuration-store.md``), dispatching on the field name AppSync reports:

``Query.getConfiguration(Profile)``
    Everything the editor needs in one read: each section's schema and cache
    tier, the shipped baseline, the profile's stored delta, the profile's
    version, which is what a save sends back, and the library of presets.

``Mutation.updateConfiguration(input)``
    Save the sections the editor changed, together, as one revision. Each is
    checked against its schema before anything is written, and the save is
    conditional on the version the editor loaded.

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

``Query.listConfigurationProfiles``
    Every profile and the baseline, without bodies: name, Managed or Custom,
    description, whether it is active, who saved it last and when.

``Mutation.createConfigurationProfile(input)``
    Create a Custom profile as a full copy of another profile (the baseline by
    default), or of an imported configuration laid over the baseline.

``Mutation.deleteConfigurationProfile(input)``
    Delete a Custom profile that is not the active one. Its history stays.

``Mutation.activateConfigurationProfile(input)``
    Make a profile, or the baseline itself, the one in force.

All are for the **Admin** group: on the AppSync field, and re-checked here.
The profile defaults to ``custom``, the profile a deployment creates. The
baseline (``default``) and Managed profiles can be read, compared and activated,
and are refused every write.
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
    InvalidConfiguration,
    ProfileExists,
    ProfileInUse,
    ProfileNotFound,
    ProfileReadOnly,
    RevisionEntry,
    RevisionNotFound,
    registered_sections,
    section_schemas,
    shipped_presets,
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
#: The deployment baseline's name, which can be read and activated, never saved.
BASELINE = "default"

GET_CONFIGURATION_FIELD = "getConfiguration"
UPDATE_CONFIGURATION_FIELD = "updateConfiguration"
LIST_FIELD = "listConfigurationRevisions"
GET_FIELD = "getConfigurationRevision"
DIFF_FIELD = "diffConfigurationRevisions"
RESTORE_FIELD = "restoreConfigurationRevision"
LABEL_FIELD = "labelConfigurationRevision"
LIST_PROFILES_FIELD = "listConfigurationProfiles"
CREATE_PROFILE_FIELD = "createConfigurationProfile"
DELETE_PROFILE_FIELD = "deleteConfigurationProfile"
ACTIVATE_PROFILE_FIELD = "activateConfigurationProfile"
FIELDS = (
    GET_CONFIGURATION_FIELD,
    UPDATE_CONFIGURATION_FIELD,
    LIST_FIELD,
    GET_FIELD,
    DIFF_FIELD,
    RESTORE_FIELD,
    LABEL_FIELD,
    LIST_PROFILES_FIELD,
    CREATE_PROFILE_FIELD,
    DELETE_PROFILE_FIELD,
    ACTIVATE_PROFILE_FIELD,
)

# Built once per container. Every read here is a fresh, consistent one: the
# editor reads the records through `read_profile`, and the history through
# `list_revisions` and `get_revision`, none of which use the store's section
# cache.
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
        LOGGER.info("refused a configuration request to %s", caller["subject"] or "(no subject)")
        raise ForbiddenError(
            f"Unauthorized: only members of the {ADMIN_GROUP} group can read or change "
            "the configuration and its history"
        )


def _field(event: Mapping[str, Any]) -> str:
    field = ((event.get("info") or {}).get("fieldName") or "").strip()
    if field not in FIELDS:
        raise ValueError(f"this resolver does not serve {field or '(nothing)'}")
    return field


def _profile(arguments: Mapping[str, Any], *, allow_baseline: bool = False) -> str:
    """The profile named, ``custom`` when none is; ``default`` only where it may be read."""
    name = str(arguments.get("Profile") or DEFAULT_PROFILE)
    if allow_baseline and name == BASELINE:
        return name
    return validate_profile_name(name)


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


def _registered_only(body: Mapping[str, Any]) -> Dict[str, Any]:
    """The sections of a body the store knows, so the editor renders nothing else."""
    known = registered_sections()
    return {name: values for name, values in body.items() if name in known}


def get_configuration(arguments: Mapping[str, Any]) -> Dict[str, Any]:
    """The editor's read: schemas, baseline, the profile's delta and its version.

    Read from the stored records with consistent reads, never from the section
    cache, so the editor shows what is stored and a save is checked against
    the version it replaces.
    """
    profile = _profile(arguments, allow_baseline=True)
    baseline = CONFIG_STORE.read_profile(BASELINE, consistent_read=True)
    head = (
        baseline
        if profile == BASELINE
        else CONFIG_STORE.read_profile(profile, consistent_read=True)
    )
    active = CONFIG_STORE.active_profile_name(consistent_read=True) or BASELINE
    specs = registered_sections()
    sections = [
        {
            "Name": name,
            # So the editor can say when a saved change takes effect.
            "CacheTier": specs[name].cache_tier.value,
            "Schema": schema,
        }
        for name, schema in section_schemas().items()
    ]
    return {
        "Profile": profile,
        "Exists": head.exists,
        "Version": head.version,
        "UpdatedBy": head.updated_by or None,
        "UpdatedAt": head.updated_at or None,
        "PublishedRevision": head.published_revision,
        # The baseline and Managed profiles are read-only: the page shows them
        # with "Copy to edit" rather than a Save.
        "Managed": profile == BASELINE or bool(head.metadata.get("Managed")),
        "Description": head.metadata.get("Description") or None,
        "Active": profile == active,
        "FullCopy": head.metadata.get("ProfileFormat") == "full",
        "Sections": json.dumps(sections, sort_keys=True),
        "Default": json.dumps(_registered_only(baseline.body), sort_keys=True),
        # For the baseline itself, the profile is empty: nothing is laid over it.
        "Custom": json.dumps(
            {} if profile == BASELINE else _registered_only(head.body), sort_keys=True
        ),
        # The configuration library: named presets the editor can apply to the
        # page as unsaved changes. See lma_config/presets.py.
        "Presets": json.dumps(shipped_presets(), sort_keys=True),
    }


def update_configuration(
    input_data: Mapping[str, Any], caller: Mapping[str, Any]
) -> Dict[str, Any]:
    """The editor's save: every changed section, in one write and one revision."""
    profile = _profile(input_data)
    expected = input_data.get("ExpectedVersion")
    if isinstance(expected, bool) or not isinstance(expected, int):
        # Required, as for a restore: the editor sends whole sections, so a save
        # made from a stale read would replace what was saved since.
        raise ValueError("ExpectedVersion is required: send the Version getConfiguration returned")
    raw = input_data.get("Sections")
    try:
        sections = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError as error:
        raise ValueError(f"Sections is not valid JSON: {error}") from error
    if not isinstance(sections, dict) or not sections:
        raise ValueError("Sections must be a JSON object naming at least one section")
    unknown = sorted(name for name in sections if name not in registered_sections())
    if unknown:
        raise InvalidConfiguration([f"{name}: is not a configuration section" for name in unknown])
    # The editor saves into a profile that exists. Creating one is a different
    # operation, with its own name to choose, and is not offered here.
    if not CONFIG_STORE.read_profile(profile, consistent_read=True).exists:
        raise ValueError(f"there is no configuration profile named {profile!r}")
    outcome = CONFIG_STORE.write_sections(
        profile,
        sections,
        updated_by=caller["actor"],
        expected_version=expected,
        comment=input_data.get("Comment"),
    )
    LOGGER.info(
        "saved %s of %s as %s (version %d)",
        ", ".join(sorted(sections)),
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


def _profile_shape(summary: Any) -> Dict[str, Any]:
    return {
        "Name": summary.name,
        "Managed": summary.managed,
        "Active": summary.active,
        "Description": summary.description or None,
        "Version": summary.version,
        "UpdatedAt": summary.updated_at or None,
        "UpdatedBy": summary.updated_by or None,
        "Origin": summary.origin or None,
        "PublishedRevision": summary.published_revision,
        "FullCopy": summary.full_copy,
    }


def list_profiles() -> Dict[str, Any]:
    return {"Profiles": [_profile_shape(p) for p in CONFIG_STORE.list_profiles()]}


def create_profile(input_data: Mapping[str, Any], caller: Mapping[str, Any]) -> Dict[str, Any]:
    name = validate_profile_name(str(input_data.get("Name") or ""))
    raw = input_data.get("Body")
    body = None
    if raw not in (None, ""):
        try:
            body = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError as error:
            raise ValueError(f"Body is not valid JSON: {error}") from error
        if not isinstance(body, dict):
            raise ValueError("Body must be a JSON object of sections")
        unknown = sorted(n for n in body if n not in registered_sections())
        if unknown:
            raise InvalidConfiguration([f"{n}: is not a configuration section" for n in unknown])
    copy_from = input_data.get("CopyFrom") or None
    if body is not None and copy_from:
        raise ValueError("send CopyFrom or Body, not both")
    outcome = CONFIG_STORE.create_profile(
        name,
        updated_by=caller["actor"],
        copy_from=str(copy_from) if copy_from else None,
        body=body,
        description=str(input_data.get("Description") or ""),
        comment=input_data.get("Comment"),
    )
    LOGGER.info("created the configuration profile %s from %s", name, copy_from or "an import")
    return {
        "Success": True,
        "Profile": name,
        "Version": outcome.version,
        "Revision": outcome.revision,
    }


def delete_profile(input_data: Mapping[str, Any]) -> Dict[str, Any]:
    name = validate_profile_name(str(input_data.get("Name") or ""))
    CONFIG_STORE.delete_profile(name)
    LOGGER.info("deleted the configuration profile %s", name)
    return {"Success": True, "Profile": name}


def activate_profile(input_data: Mapping[str, Any]) -> Dict[str, Any]:
    name = _profile({"Profile": input_data.get("Name") or ""}, allow_baseline=True)
    CONFIG_STORE.activate_profile(name)
    LOGGER.info("activated the configuration profile %s", name)
    return {"Success": True, "Profile": name}


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """AppSync entry point."""
    field = _field(event)
    caller = _caller(event)
    # Checked before the table is read, and outside the handling below so the
    # refusal reaches the client as itself.
    _require_admin(caller)
    arguments = event.get("arguments") or {}
    try:
        if field == GET_CONFIGURATION_FIELD:
            return get_configuration(arguments)
        if field == UPDATE_CONFIGURATION_FIELD:
            return update_configuration(arguments.get("input") or {}, caller)
        if field == LIST_FIELD:
            return list_revisions(arguments)
        if field == GET_FIELD:
            return get_revision(arguments)
        if field == DIFF_FIELD:
            return diff_revisions(arguments)
        if field == RESTORE_FIELD:
            return restore_revision(arguments.get("input") or {}, caller)
        if field == LIST_PROFILES_FIELD:
            return list_profiles()
        if field == CREATE_PROFILE_FIELD:
            return create_profile(arguments.get("input") or {}, caller)
        if field == DELETE_PROFILE_FIELD:
            return delete_profile(arguments.get("input") or {})
        if field == ACTIVATE_PROFILE_FIELD:
            return activate_profile(arguments.get("input") or {})
        return label_revision(arguments.get("input") or {})
    except ConcurrentModification as error:
        # The prefix the configuration pages match on to tell the admin to
        # reload. The message names whoever saved the profile, which belongs in
        # the answer to the admin, not in CloudWatch.
        LOGGER.info("refused a %s against a superseded profile", field)
        raise Exception(f"Conflict: {error}") from error  # pylint: disable=broad-exception-raised
    except InvalidConfiguration as error:
        # The prefix the web UI's Configuration page matches on, to show a value's
        # problems beside the save rather than as a failure to try again. Only
        # schema refusals carry it, a section name that has no schema included.
        # (A registered section that ships no schema is a packaging fault, not
        # caught here, and reaches the client as itself.)
        raise Exception(f"Invalid: {error}") from error  # pylint: disable=broad-exception-raised
    except (RevisionNotFound, ProfileNotFound) as error:
        raise Exception(f"Not found: {error}") from error  # pylint: disable=broad-exception-raised
    except (ProfileExists, ProfileInUse, ProfileReadOnly) as error:
        # Refusals the page shows as they are: a name taken, the active profile,
        # or a profile the deployment manages.
        raise Exception(f"Refused: {error}") from error  # pylint: disable=broad-exception-raised
