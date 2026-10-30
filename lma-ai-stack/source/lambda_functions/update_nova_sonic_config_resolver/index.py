"""
Lambda resolver for the Nova Sonic voice-assistant settings: the read and the write.

Serves ``Query.getNovaSonicConfig`` and ``Mutation.updateNovaSonicConfig``,
dispatching on the field name AppSync reports. Both are served here because the
settings live in the shared configuration table, which stores a record's body
compressed, and an AppSync JavaScript resolver has no way to decompress one.

What it stores: the fields on the allowed list below, with enum-like values
checked and text fields trimmed to a bounded length, as the ``custom`` profile's
``nova_sonic_config`` section. The store records who saved it and when.
Membership of the Admin group is re-checked here as well as on the AppSync
field.

A save carries the version the page was looking at. If the profile moved on in
between -- another administrator saving, on this page or another configuration
page, since every page's settings share the one profile -- the write is refused
and the message says to reload, rather than the later save silently replacing
the earlier one.

Copyright (c) 2025 Amazon.com
This file is licensed under the MIT License.
"""

import json
import os
from typing import Any, Dict, Optional

# pylint: disable=import-error
from lma_config import (  # from the configuration layer
    NOVA_SONIC_CONFIG,
    ConcurrentModification,
    ConfigurationStore,
)

# pylint: enable=import-error

#: The profile an operator's edits live in.
OPERATOR_PROFILE = "custom"

#: The identifiers the GraphQL fields have always used, kept so the page and any
#: existing client keep working: one names the deployment baseline, the other
#: the operator's profile.
DEFAULT_CONFIG_ID = "DefaultNovaSonicConfig"
CONFIG_ID = "CustomNovaSonicConfig"

QUERY_FIELD = "getNovaSonicConfig"
MUTATION_FIELD = "updateNovaSonicConfig"

ADMIN_GROUP = os.environ.get("ADMIN_GROUP", "Admin")

# Allowlist of valid Nova Sonic configuration fields. The configuration seeder
# carries exactly these across from the previous table on upgrade;
# test_the_seeder_adopts_exactly_the_fields_this_resolver_accepts holds the two
# lists equal.
ALLOWED_FIELDS = {
    "systemPrompt",
    "promptMode",
    "modelId",
    "voiceId",
    "endpointingSensitivity",
    "groupMeetingMode",  # legacy; preserved for back-compat
    "meetingMode",  # new canonical meeting-mode selector
    "translatorLanguageA",  # only relevant when meetingMode='translator'
    "translatorLanguageB",  # only relevant when meetingMode='translator'
    "translatorMutePhrases",  # comma-separated; translator-mode pause triggers
    "translatorUnmutePhrases",  # comma-separated; translator-mode resume triggers
}

# Valid values for enum-like fields
VALID_PROMPT_MODES = {"base", "inject", "replace"}
VALID_SENSITIVITY_LEVELS = {"LOW", "MEDIUM", "HIGH"}
VALID_MEETING_MODES = {"normal", "group", "translator"}

# Maximum length for translator language labels (prevents huge strings in config).
MAX_LANGUAGE_LABEL_LEN = 64

# Maximum length for the comma-separated translator trigger-phrase strings.
MAX_TRIGGER_PHRASES_LEN = 256

# Enum-like fields and the set of values each accepts. Every key must also be in
# ALLOWED_FIELDS, which a test asserts.
ENUM_FIELDS = {
    "promptMode": VALID_PROMPT_MODES,
    "endpointingSensitivity": VALID_SENSITIVITY_LEVELS,
    "meetingMode": VALID_MEETING_MODES,
}

# Free-text fields and the length each is trimmed to. Every key must also be in
# ALLOWED_FIELDS, which a test asserts.
BOUNDED_TEXT_FIELDS = {
    "translatorLanguageA": MAX_LANGUAGE_LABEL_LEN,
    "translatorLanguageB": MAX_LANGUAGE_LABEL_LEN,
    "translatorMutePhrases": MAX_TRIGGER_PHRASES_LEN,
    "translatorUnmutePhrases": MAX_TRIGGER_PHRASES_LEN,
}

# Built once per container so the DynamoDB client is reused. Nothing here is
# served from its cache: the read calls read_profile, which always reads, and
# reads strongly consistent -- the page has to show what is stored, and a save
# has to see the version it is replacing.
CONFIG_STORE = ConfigurationStore()


class ForbiddenError(Exception):
    """Raised when the caller is not a member of the required group."""


def _get_caller_identity(event: Dict[str, Any]) -> Dict[str, Any]:
    """Who is asking, from the AppSync `identity` block.

    AppSync builds `identity` from the validated user-pool token, so it is the
    only source of group membership consulted here; the mutation arguments are
    not. `cognito:groups` arrives as a list from the user-pool authorizer and as
    a comma-joined string from some token shapes, so both are normalized. Any
    other shape, including an absent claims block, yields no groups -- which
    `_require_admin` refuses.
    """
    identity = event.get("identity") or {}
    claims = identity.get("claims") or {}
    groups = claims.get("cognito:groups") or identity.get("groups") or []
    if isinstance(groups, str):
        groups = [group.strip() for group in groups.split(",") if group.strip()]
    elif not isinstance(groups, (list, tuple)):
        groups = []
    actor = (
        claims.get("email")
        or claims.get("cognito:username")
        or identity.get("username")
        or claims.get("sub")
        or identity.get("sub")
        or ""
    )
    # Deliberately the Cognito subject alone, with no fall back to an email or a
    # username that may be one. It identifies the caller in a log line without
    # putting an address into CloudWatch, whose retention is a deployment-wide
    # setting; `actor` above carries the address, and the UpdatedBy attribute on
    # the stored item is where the durable record of who changed what belongs.
    subject = claims.get("sub") or identity.get("sub") or ""
    return {
        "actor": actor,
        "subject": subject,
        "groups": list(groups),
        "is_admin": ADMIN_GROUP in groups,
        # An IAM-signed request carries the signing principal's ARN and no
        # user-pool claims. AppSync builds this block itself, so a client cannot
        # make a user-pool request look like one.
        "is_iam": bool(identity.get("userArn")) and not claims,
    }


def _require_admin(caller: Dict[str, Any], action: str) -> None:
    """Re-check the caller's group membership inside the function.

    Keeps the schema directive from being the only place the constraint exists.
    """
    if not caller["is_admin"]:
        print(
            f"Caller {caller['subject'] or '(no subject claim)'} is not a member of "
            f"the {ADMIN_GROUP} group (groups={caller['groups']})"
        )
        raise ForbiddenError(
            f"Unauthorized: only members of the {ADMIN_GROUP} group can {action} "
            "the Nova Sonic configuration"
        )


def _require_reader(caller: Dict[str, Any]) -> None:
    """The read's two authorization modes, as the schema declares them.

    `getNovaSonicConfig` is open to the Admin group on user-pool authentication
    and to IAM-signed requests, which AppSync has already authorized against the
    signer's policy. A user-pool caller outside the group is refused here as
    well as on the field.
    """
    if caller["is_iam"]:
        return
    _require_admin(caller, "read")


def _field_name(event: Dict[str, Any]) -> str:
    """Which GraphQL field this invocation is for.

    Both resolvers pass `ctx.info.fieldName`. Absent, the request is refused
    rather than guessed at: guessing from the argument shape would mean a read
    that happened to look like a write.
    """
    field = ((event.get("info") or {}).get("fieldName") or "").strip()
    if field not in (QUERY_FIELD, MUTATION_FIELD):
        raise ValueError(
            f"this resolver serves {QUERY_FIELD} and {MUTATION_FIELD}; the request "
            f"named {field or '(nothing)'}"
        )
    return field


def _bounded_text(key: str, value: Any, limit: int) -> Optional[str]:
    """A non-empty trimmed string no longer than `limit`, or None to drop the field."""
    if not isinstance(value, str):
        print(f"Invalid {key} (not a string): {value!r}, skipping")
        return None
    value = value.strip()
    if not value:
        print(f"Invalid {key} (empty string), skipping")
        return None
    if len(value) > limit:
        print(f"Invalid {key} (length {len(value)} > {limit}), truncating")
        value = value[:limit]
    return value


def _validated_field(key: str, value: Any) -> Optional[Any]:
    """The value to store for `key`, or None to drop the field.

    Dropping rather than failing the whole save matches the other config
    resolvers: one stale value from an older client should not block a save.
    """
    if key not in ALLOWED_FIELDS:
        print(f"Filtered out non-allowed field: {key}")
        return None
    if key in ENUM_FIELDS:
        # An unrecognised value is dropped rather than stored.
        if value in ENUM_FIELDS[key]:
            return value
        print(f"Invalid {key} value: {value}, skipping")
        return None
    if key in BOUNDED_TEXT_FIELDS:
        return _bounded_text(key, value, BOUNDED_TEXT_FIELDS[key])
    if key == "groupMeetingMode":
        return bool(value)
    return value


def _read(config_id: str) -> Dict[str, Any]:
    """Serve the read: the record's settings plus what the page needs to save.

    The GraphQL field answers with a JSON string, as it always has. What is in
    it is the section's settings plus ``Version``, ``UpdatedBy`` and
    ``UpdatedAt`` -- the partition key, the seeded guidance text and the
    description the previous table's item carried are not in it. ``Version``
    is what the page sends back as ``ExpectedVersion``.
    """
    # Two identifiers, and nothing else: an unrecognised one is refused rather
    # than falling back to the baseline, which would answer a typo with a
    # plausible-looking record.
    if config_id == CONFIG_ID:
        profile = OPERATOR_PROFILE
    elif config_id == DEFAULT_CONFIG_ID:
        profile = "default"
    else:
        raise ValueError(
            f"Unknown NovaSonicConfigId {config_id!r}; expected {DEFAULT_CONFIG_ID} or {CONFIG_ID}"
        )
    record = CONFIG_STORE.read_profile(profile, consistent_read=True)
    section = record.body.get(NOVA_SONIC_CONFIG.name)
    settings: Dict[str, Any] = dict(section) if isinstance(section, dict) else {}
    payload: Dict[str, Any] = dict(settings)
    payload["Version"] = record.version
    if record.updated_by:
        payload["UpdatedBy"] = record.updated_by
    if record.updated_at:
        payload["UpdatedAt"] = record.updated_at
    print(
        f"Read Nova Sonic config: id={config_id} profile={profile} "
        f"version={record.version} fields={sorted(settings)}"
    )
    return {"NovaSonicConfigId": json.dumps(payload)}


def _write(input_data: Dict[str, Any], caller: Dict[str, Any]) -> Dict[str, Any]:
    """Serve the write: store the allowed fields as the profile's Nova Sonic section."""
    config_id = input_data["NovaSonicConfigId"]
    config_str = input_data["ConfigData"]

    # Only the operator's profile is editable; the baseline is written by the
    # stack's configuration seeder and is what a reset falls back to.
    if config_id != CONFIG_ID:
        raise ValueError(f"Only {CONFIG_ID} can be updated")

    try:
        config_object = json.loads(config_str)
    except json.JSONDecodeError as error:
        raise ValueError(f"Invalid JSON in ConfigData: {error}") from error
    if not isinstance(config_object, dict):
        raise ValueError("ConfigData must be a JSON object")

    settings: Dict[str, Any] = {}
    for key, value in config_object.items():
        validated = _validated_field(key, value)
        if validated is not None:
            settings[key] = validated

    expected_version = input_data.get("ExpectedVersion")
    if expected_version is not None:
        expected_version = int(expected_version)

    # One section, not the whole record: the profile holds every configuration
    # page's settings, and replacing the body would delete the others.
    version = CONFIG_STORE.write_section(
        OPERATOR_PROFILE,
        NOVA_SONIC_CONFIG.name,
        settings,
        updated_by=caller["actor"],
        expected_version=expected_version,
    )
    print(f"Successfully updated Nova Sonic config: fields={sorted(settings)} version={version}")
    return {"NovaSonicConfigId": config_id, "Success": True, "Version": version}


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """AppSync resolver entry point for both the read and the write.

    Args:
        event: AppSync event with arguments, identity and info.
        context: Lambda context.

    Returns:
        The GraphQL field's shape: the stored payload for the read, the
        identifier, outcome and new version for the write.
    """
    caller = _get_caller_identity(event)
    field = _field_name(event)

    if field == QUERY_FIELD:
        _require_reader(caller)
        try:
            return _read(event["arguments"]["NovaSonicConfigId"])
        except Exception as error:
            print(f"Error reading Nova Sonic config: {error}")
            raise Exception(f"Failed to read Nova Sonic config: {error}") from error

    # Checked before the table is touched, and deliberately outside the catch-all
    # below so the refusal reaches the client as itself rather than as a save
    # failure. The AppSync resolver unit turns it into the error for this field.
    _require_admin(caller, "update")

    try:
        return _write(event["arguments"]["input"], caller)
    except ConcurrentModification as error:
        # Surfaced as itself: the admin has to be told their copy is stale, not
        # that the save failed for an unknown reason.
        # A fixed line: the message names whoever saved the record,
        # which belongs in the error returned to the admin and on the
        # record's UpdatedBy, not in CloudWatch.
        print("Refused a save against a superseded record")
        raise Exception(f"Conflict: {error}") from error
    except Exception as error:
        print(f"Error updating Nova Sonic config: {error}")
        raise Exception(f"Failed to update Nova Sonic config: {error}") from error
