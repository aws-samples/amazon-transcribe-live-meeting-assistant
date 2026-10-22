# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""AppSync Lambda resolver for the updateAsrConfig mutation.

The on-demand ASR engine has exactly three runtime switches, all booleans, all
read at the start of each meeting so a change needs no stack update:

* streamingEngineMicrovm - streaming meetings (Stream Audio, the Chrome extension,
  the Desktop Capture apps) use the on-demand engine instead of Amazon Transcribe.
* virtualParticipantEngineMicrovm - Virtual Participants use the on-demand engine.
* diarizeVirtualParticipant - a Virtual Participant on the on-demand engine asks
  for per-voice labels, so several people behind one attendee tile come out as
  "Name (spk_0)", "Name (spk_1)" instead of one name.

There are deliberately no tuning fields. The diarization operating point (the
similarity threshold and the minimum utterance length) is measured for the model
bundle and baked into the ASR image; it is not something a deployment should have
to know a number for, and a guessed value fragments or merges speakers.
"""

import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import boto3

dynamodb = boto3.resource("dynamodb")

CONFIG_ID = "CustomAsrConfig"

ADMIN_GROUP = os.environ.get("ADMIN_GROUP", "Admin")

BOOLEAN_FIELDS = frozenset(
    {"streamingEngineMicrovm", "virtualParticipantEngineMicrovm", "diarizeVirtualParticipant"}
)
ALLOWED_FIELDS = BOOLEAN_FIELDS


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
            "the ASR configuration"
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


def _as_switch(value: Any) -> Optional[bool]:
    """A boolean, or the strings "true"/"false" in any case; anything else is None."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
    return None


def lambda_handler(event: dict, context: Any) -> dict:
    """Validate and store the ASR runtime switches.

    Returns ``{"AsrConfigId", "Success"}``. Raises only when the request itself is
    unusable; an unknown field is dropped with a log line rather than failing the
    whole save, matching the other config resolvers.
    """
    caller = _get_caller_identity(event)
    # Checked before the table is touched, and deliberately outside the catch-all
    # below so the refusal reaches the client as itself rather than as a save
    # failure. The AppSync resolver unit turns it into the error for this field.
    _require_admin(caller)

    try:
        table = dynamodb.Table(os.environ["ASR_CONFIG_TABLE_NAME"])

        input_data = event["arguments"]["input"]
        config_id = input_data["AsrConfigId"]
        if config_id != CONFIG_ID:
            raise ValueError(f"Only {CONFIG_ID} can be updated")

        try:
            config_object = json.loads(input_data["ConfigData"])
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON in ConfigData: {exc}") from exc
        if not isinstance(config_object, dict):
            raise ValueError("ConfigData must be a JSON object")

        item: dict[str, Any] = {"AsrConfigId": config_id}
        for key, value in config_object.items():
            if key not in ALLOWED_FIELDS:
                print(f"Filtered out non-allowed field: {key}")
                continue
            parsed = _as_switch(value)
            if parsed is None:
                print(f"Filtered out non-boolean value for {key}: {value!r}")
                continue
            item[key] = parsed

        item.update(_audit_attributes(caller))

        table.put_item(Item=item)
        # The switches only. Logging the whole item would put the saver's email
        # address into CloudWatch; UpdatedBy on the item is the record of who
        # saved it, and it is already durable there.
        switches = {key: str(item[key]) for key in sorted(ALLOWED_FIELDS) if key in item}
        print(f"Updated ASR config {config_id}: {json.dumps(switches)}")
        return {"AsrConfigId": config_id, "Success": True}

    except Exception as exc:
        print(f"Error updating ASR config: {exc}")
        raise Exception(f"Failed to update ASR config: {exc}") from exc
