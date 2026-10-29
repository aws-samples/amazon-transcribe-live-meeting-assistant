# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""AppSync Lambda resolver for the ASR runtime switches: the read and the write.

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

Serves ``Query.getAsrConfig`` and ``Mutation.updateAsrConfig``, dispatching on the
field name AppSync reports. The switches live in the shared configuration table,
as the ``asr_engine`` section, which stores a record's body compressed; an AppSync
JavaScript resolver has no way to decompress one.

The read is open to every signed-in user and to IAM-signed requests: the Stream
Audio and Upload Audio forms read which engine is selected, and the Virtual
Participant reads its two switches with SigV4. It answers with the *effective*
switches -- the shipped baseline with the operator's profile over it -- read
directly and consistently, so the Virtual Participant's path caches nothing, as it
never has. The write is for the Admin group, re-checked here, and is conditional
on the version the page loaded.
"""

import json
import os
from typing import Any, Dict, Optional

# pylint: disable=import-error
from lma_config import (  # from the configuration layer
    ASR_ENGINE,
    ConcurrentModification,
    ConfigurationStore,
    deep_merge,
)

# pylint: enable=import-error

#: The profile an operator's edits live in.
OPERATOR_PROFILE = "custom"

CONFIG_ID = "CustomAsrConfig"

QUERY_FIELD = "getAsrConfig"
MUTATION_FIELD = "updateAsrConfig"

ADMIN_GROUP = os.environ.get("ADMIN_GROUP", "Admin")

BOOLEAN_FIELDS = frozenset(
    {"streamingEngineMicrovm", "virtualParticipantEngineMicrovm", "diarizeVirtualParticipant"}
)
ALLOWED_FIELDS = BOOLEAN_FIELDS

# Built once per container so the DynamoDB client is reused. Nothing here is
# served from its cache: both paths call read_profile, which always reads, and
# reads strongly consistent.
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
            "the ASR configuration"
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


def _section(body: Dict[str, Any]) -> Dict[str, Any]:
    values = body.get(ASR_ENGINE.name)
    return dict(values) if isinstance(values, dict) else {}


def _read(config_id: str) -> Dict[str, Any]:
    """Serve the read: the effective switches, plus the profile's version.

    Only the three switches are returned, typed as booleans; ``Version`` is what
    the Transcription Engine page sends back as ``ExpectedVersion``.
    """
    # One identifier, and nothing else: an unrecognised one is refused rather
    # than answered with plausible-looking switches.
    if config_id != CONFIG_ID:
        raise ValueError(f"Unknown AsrConfigId {config_id!r}; expected {CONFIG_ID}")
    baseline = CONFIG_STORE.read_profile("default", consistent_read=True)
    profile = CONFIG_STORE.read_profile(OPERATOR_PROFILE, consistent_read=True)
    effective = deep_merge(_section(baseline.body), _section(profile.body))
    switches = {
        key: effective[key]
        for key in sorted(BOOLEAN_FIELDS)
        if isinstance(effective.get(key), bool)
    }
    print(f"Read ASR config: version={profile.version} switches={json.dumps(switches)}")
    return {"AsrConfigId": config_id, **switches, "Version": profile.version}


def _write(input_data: Dict[str, Any], caller: Dict[str, Any]) -> Dict[str, Any]:
    """Serve the write: store the valid switches as the profile's ASR section."""
    config_id = input_data["AsrConfigId"]
    if config_id != CONFIG_ID:
        raise ValueError(f"Only {CONFIG_ID} can be updated")

    try:
        config_object = json.loads(input_data["ConfigData"])
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in ConfigData: {exc}") from exc
    if not isinstance(config_object, dict):
        raise ValueError("ConfigData must be a JSON object")

    switches: Dict[str, bool] = {}
    for key, value in config_object.items():
        if key not in ALLOWED_FIELDS:
            print(f"Filtered out non-allowed field: {key}")
            continue
        parsed = _as_switch(value)
        if parsed is None:
            print(f"Filtered out non-boolean value for {key}: {value!r}")
            continue
        switches[key] = parsed

    expected_version = input_data.get("ExpectedVersion")
    if expected_version is not None:
        expected_version = int(expected_version)

    # One section, not the whole record: the profile holds every configuration
    # page's settings, and replacing the body would delete the others.
    version = CONFIG_STORE.write_section(
        OPERATOR_PROFILE,
        ASR_ENGINE.name,
        switches,
        updated_by=caller["actor"],
        expected_version=expected_version,
    )
    # The switches only. Logging the caller would put the saver's email address
    # into CloudWatch; UpdatedBy on the record is where that is kept.
    print(f"Updated ASR config {config_id}: {json.dumps(switches)} version={version}")
    return {"AsrConfigId": config_id, "Success": True, "Version": version}


def lambda_handler(event: dict, context: Any) -> dict:
    """AppSync resolver entry point for both the read and the write.

    Raises only when the request itself is unusable; an unknown field is dropped
    with a log line rather than failing the whole save, matching the other
    config resolvers.
    """
    field = _field_name(event)

    if field == QUERY_FIELD:
        # No group check: the Stream Audio and Upload Audio forms read this for
        # every user, and the Virtual Participant with SigV4. The field's
        # directives are the authorization.
        try:
            return _read(event["arguments"]["AsrConfigId"])
        except Exception as exc:
            print(f"Error reading ASR config: {exc}")
            raise Exception(f"Failed to read ASR config: {exc}") from exc

    caller = _get_caller_identity(event)
    # Checked before the table is touched, and deliberately outside the catch-all
    # below so the refusal reaches the client as itself rather than as a save
    # failure. The AppSync resolver unit turns it into the error for this field.
    _require_admin(caller)

    try:
        return _write(event["arguments"]["input"], caller)
    except ConcurrentModification as exc:
        # Surfaced as itself: the admin has to be told their copy is stale, not
        # that the save failed for an unknown reason.
        # A fixed line: the message names whoever saved the record,
        # which belongs in the error returned to the admin and on the
        # record's UpdatedBy, not in CloudWatch.
        print("Refused a save against a superseded record")
        raise Exception(f"Conflict: {exc}") from exc
    except Exception as exc:
        print(f"Error updating ASR config: {exc}")
        raise Exception(f"Failed to update ASR config: {exc}") from exc
