"""
Lambda resolver for the Meeting Assist chat buttons: both the read and the write.

Serves ``Query.getChatButtonConfig`` and ``Mutation.updateChatButtonConfig``,
dispatching on the field name AppSync reports. Both are served here because the
buttons live in the shared configuration table, which stores a record's body
compressed, and an AppSync JavaScript resolver has no way to decompress one.

The read is open to every signed-in user and to IAM-signed requests, because the
chat panel renders its buttons from it for whoever is viewing a meeting. It
answers with the record's buttons and its version, and nothing else: the record
also says who saved it, and that is not something every reader of the buttons
asked for.

The write stores the "N#Label" fields as the ``custom`` profile's
``chat_buttons`` section; the store records who saved it and when. Membership of
the Admin group is re-checked here as well as on the AppSync field. A save
carries the version the dialog loaded, and is refused if the profile moved on in
between -- another administrator saving, from this dialog or another
configuration page, since every page's settings share the one profile.

Copyright (c) 2025 Amazon.com
This file is licensed under the MIT License.
"""

import json
import os
import re
from typing import Any, Dict

# pylint: disable=import-error
from lma_config import (  # from the configuration layer
    CHAT_BUTTONS,
    ConcurrentModification,
    ConfigurationStore,
)

# pylint: enable=import-error

#: The profile an operator's edits live in.
OPERATOR_PROFILE = "custom"

#: The identifiers the GraphQL fields have always used, kept so the chat panel
#: and any existing client keep working: one names the shipped buttons, the
#: other the operator's.
DEFAULT_CONFIG_ID = "DefaultChatButtonConfig"
CONFIG_ID = "CustomChatButtonConfig"

QUERY_FIELD = "getChatButtonConfig"
MUTATION_FIELD = "updateChatButtonConfig"

ADMIN_GROUP = os.environ.get("ADMIN_GROUP", "Admin")

# Allowlist of valid chat button fields, in "N#LABEL" form -- "1#Action Items",
# "2#Summary" and so on, with ASCII digits. The stored section holds exactly
# these, and the read returns exactly these. The configuration seeder carries
# the same form across from the previous table on upgrade.
BUTTON_FIELD_PATTERN = re.compile(r"^[0-9]+#")

# Built once per container so the DynamoDB client is reused. Nothing here is
# served from its cache: the read calls read_profile, which always reads, and
# reads strongly consistent -- the dialog shows the buttons straight after a
# save, and a save has to see the version it is replacing.
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
            "the chat button configuration"
        )


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


def _buttons_of(values: Any) -> Dict[str, Any]:
    """The "N#Label" entries of a section, and nothing else."""
    if not isinstance(values, dict):
        return {}
    return {key: value for key, value in values.items() if BUTTON_FIELD_PATTERN.match(key)}


def _read(config_id: str) -> Dict[str, Any]:
    """Serve the read: one record's buttons, plus the version the dialog saves against.

    The two records are answered separately, never merged, because a non-empty
    custom list replaces the shipped one rather than overriding it key by key;
    the chat panel decides which applies. ``Version`` is the only key in the
    payload that is not a button, and the chat panel's own "N#Label" filter
    drops it -- so a reader that ignores it sees the same buttons as before.
    """
    # Two identifiers, and nothing else: an unrecognised one is refused rather
    # than falling back to the shipped buttons, which would answer a typo with a
    # plausible-looking record.
    if config_id == CONFIG_ID:
        profile = OPERATOR_PROFILE
    elif config_id == DEFAULT_CONFIG_ID:
        profile = "default"
    else:
        raise ValueError(
            f"Unknown ChatButtonConfigId {config_id!r}; expected {DEFAULT_CONFIG_ID} or {CONFIG_ID}"
        )
    record = CONFIG_STORE.read_profile(profile, consistent_read=True)
    buttons = _buttons_of(record.body.get(CHAT_BUTTONS.name))
    payload: Dict[str, Any] = dict(buttons)
    payload["Version"] = record.version
    print(
        f"Read chat buttons: id={config_id} profile={profile} "
        f"version={record.version} buttons={len(buttons)}"
    )
    return {"ChatButtonConfigId": json.dumps(payload)}


def _write(input_data: Dict[str, Any], caller: Dict[str, Any]) -> Dict[str, Any]:
    """Serve the write: store the button fields as the profile's chat-button section."""
    config_id = input_data["ChatButtonConfigId"]
    button_config_str = input_data["ButtonConfig"]

    # Only the operator's profile is editable; the shipped buttons are written by
    # the stack's configuration seeder and are what a reset falls back to.
    if config_id != CONFIG_ID:
        raise ValueError(f"Only {CONFIG_ID} can be updated")

    try:
        config_object = json.loads(button_config_str)
    except json.JSONDecodeError as error:
        raise ValueError(f"Invalid JSON in ButtonConfig: {error}") from error
    if not isinstance(config_object, dict):
        raise ValueError("ButtonConfig must be a JSON object")

    buttons: Dict[str, Any] = {}
    for key, value in config_object.items():
        if BUTTON_FIELD_PATTERN.match(key):
            buttons[key] = value
        else:
            print(f"Filtered out non-button field: {key}")

    expected_version = input_data.get("ExpectedVersion")
    if expected_version is not None:
        expected_version = int(expected_version)

    # One section, not the whole record: the profile holds every configuration
    # page's settings, and replacing the body would delete the others.
    version = CONFIG_STORE.write_section(
        OPERATOR_PROFILE,
        CHAT_BUTTONS.name,
        buttons,
        updated_by=caller["actor"],
        expected_version=expected_version,
        # Recorded on the revision the save creates; see docs/configuration-store.md.
        comment=input_data.get("Comment"),
    )
    print(f"Successfully updated chat buttons: {len(buttons)} button(s), version={version}")
    return {"ChatButtonConfigId": config_id, "Success": True, "Version": version}


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """AppSync resolver entry point for both the read and the write.

    Args:
        event: AppSync event with arguments, identity and info.
        context: Lambda context.

    Returns:
        The GraphQL field's shape: the stored buttons for the read, the
        identifier, outcome and new version for the write.
    """
    field = _field_name(event)

    if field == QUERY_FIELD:
        # No group check: every signed-in user's chat panel reads this, and the
        # field's directives are the authorization (see the module docstring).
        try:
            return _read(event["arguments"]["ChatButtonConfigId"])
        except Exception as error:
            print(f"Error reading chat button config: {error}")
            raise Exception(f"Failed to read chat button config: {error}") from error

    caller = _get_caller_identity(event)
    # Checked before the table is touched, and deliberately outside the catch-all
    # below so the refusal reaches the client as itself rather than as a save
    # failure. The AppSync resolver unit turns it into the error for this field.
    _require_admin(caller)

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
        print(f"Error updating chat button config: {error}")
        raise Exception(f"Failed to update chat button config: {error}") from error
