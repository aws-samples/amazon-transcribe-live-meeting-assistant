"""
Lambda function resolver for updateNovaSonicConfig mutation

Stores the custom Nova Sonic record: the fields on the allowed list below, with
enum-like values checked and text fields trimmed to a bounded length, plus the
partition key and the audit attributes naming who saved it and when. Membership
of the Admin group is re-checked here as well as on the AppSync field.

Copyright (c) 2025 Amazon.com
This file is licensed under the MIT License.
"""

import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import boto3

dynamodb = boto3.resource("dynamodb")

CONFIG_ID = "CustomNovaSonicConfig"

ADMIN_GROUP = os.environ.get("ADMIN_GROUP", "Admin")

# Allowlist of valid Nova Sonic configuration fields
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
    }


def _require_admin(caller: Dict[str, Any]) -> None:
    """Re-check the caller's group membership inside the function.

    Keeps the schema directive from being the only place the constraint exists.
    """
    if not caller["is_admin"]:
        print(
            f"Caller {caller['subject'] or '(no subject claim)'} is not a member of "
            f"the {ADMIN_GROUP} group (groups={caller['groups']})"
        )
        raise ForbiddenError(
            f"Unauthorized: only members of the {ADMIN_GROUP} group can update "
            "the Nova Sonic configuration"
        )


def _audit_attributes(caller: Dict[str, Any]) -> Dict[str, str]:
    """Who saved this record and when, in ISO-8601 UTC.

    Written from the verified identity, never from the request: the allowed
    field list drops these names if a client sends them.
    """
    return {
        "UpdatedBy": caller["actor"],
        "UpdatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


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


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    AppSync Lambda resolver for updateNovaSonicConfig

    Validates and filters input to only allow known Nova Sonic configuration fields.

    Args:
        event: AppSync event with arguments and identity
        context: Lambda context

    Returns:
        Dict with NovaSonicConfigId and Success status
    """
    caller = _get_caller_identity(event)
    # Checked before the table is touched, and deliberately outside the catch-all
    # below so the refusal reaches the client as itself rather than as a save
    # failure. The AppSync resolver unit turns it into the error for this field.
    _require_admin(caller)

    try:
        # Get table name from environment
        table_name = os.environ["NOVA_SONIC_CONFIG_TABLE_NAME"]
        table = dynamodb.Table(table_name)

        # Extract input from AppSync event
        input_data = event["arguments"]["input"]
        config_id = input_data["NovaSonicConfigId"]
        config_str = input_data["ConfigData"]

        # Only the Custom record is editable; the Default one is seeded by the
        # nova-sonic-config stack and is what a reset falls back to.
        if config_id != CONFIG_ID:
            raise ValueError(f"Only {CONFIG_ID} can be updated")

        # Parse the JSON configuration
        try:
            config_object = json.loads(config_str)
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON in ConfigData: {str(e)}")
        if not isinstance(config_object, dict):
            raise ValueError("ConfigData must be a JSON object")

        # Build item with only allowed fields
        item = {"NovaSonicConfigId": config_id}

        for key, value in config_object.items():
            validated = _validated_field(key, value)
            if validated is not None:
                item[key] = validated

        item.update(_audit_attributes(caller))

        # Store in DynamoDB
        table.put_item(Item=item)

        print(f"Successfully updated Nova Sonic config: {config_id}")

        return {"NovaSonicConfigId": config_id, "Success": True}

    except Exception as e:
        print(f"Error updating Nova Sonic config: {str(e)}")
        raise Exception(f"Failed to update Nova Sonic config: {str(e)}")
