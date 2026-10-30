"""
Lambda resolver for the summary prompt templates: both the read and the write.

Serves ``Query.getLLMPromptTemplate`` and ``Mutation.updateLLMPromptTemplate``,
dispatching on the field name AppSync reports. The read used to be a direct
DynamoDB resolver; it is here now because the shared configuration table stores
a record's body compressed, and an AppSync JavaScript resolver has no way to
decompress one.

What it stores: the fields on the allowed list below, as the ``custom``
profile's summary-prompt section, plus the audit attributes naming who saved
them and when. Membership of the Admin group is re-checked here as well as on
the AppSync field.

A save carries the version the editor was looking at. If the record moved on in
between -- another administrator saving the same page -- the write is refused
and the message says to reload, rather than the later save silently replacing
the earlier one.

Copyright (c) 2025 Amazon.com
This file is licensed under the MIT License.
"""

import json
import os
import re
from typing import Any, Dict

# pylint: disable=import-error
from lma_config import (  # from the configuration layer
    SUMMARY_PROMPT_TEMPLATES,
    ConcurrentModification,
    ConfigurationStore,
)

# pylint: enable=import-error

#: The profile an operator's edits live in.
OPERATOR_PROFILE = "custom"

#: The identifiers the GraphQL field has always used, kept so the UI and any
#: existing client keep working: one names the deployment baseline, the other
#: the operator's profile.
DEFAULT_CONFIG_ID = "DefaultSummaryPromptTemplates"
CUSTOM_CONFIG_ID = "CustomSummaryPromptTemplates"

QUERY_FIELD = "getLLMPromptTemplate"
MUTATION_FIELD = "updateLLMPromptTemplate"

ADMIN_GROUP = os.environ.get("ADMIN_GROUP", "Admin")

# Allowlist of valid prompt template fields, in "N#LABEL" form -- "1#SUMMARY",
# "2#DETAILS", "3#ACTIONS" and so on. The stored section holds exactly these.
TEMPLATE_FIELD_PATTERN = re.compile(r"^\d+#")

# Built once per container so the DynamoDB client is reused. Nothing here is
# served from its cache: both paths call read_profile, which always reads, and
# reads strongly consistent -- an editor has to show what is stored, and a save
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
            f"Unauthorized: only members of the {ADMIN_GROUP} group can read or "
            "update the summary prompt templates"
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


def _prompts_of(record_body: Dict[str, Any]) -> Dict[str, Any]:
    """The prompt section of a record body.

    The fallback is for a body with no section at all, holding its prompts at
    the top level. Nothing writes one -- the seeder and this resolver both write
    the section -- so it covers a record repaired by hand in the console, where
    answering the editor with nothing would be the more confusing outcome.
    """
    section = record_body.get(SUMMARY_PROMPT_TEMPLATES.name)
    if isinstance(section, dict):
        return section
    return {key: value for key, value in record_body.items() if TEMPLATE_FIELD_PATTERN.match(key)}


def _read(config_id: str) -> Dict[str, Any]:
    """Serve the read: the record's prompts plus what the editor needs to save.

    ``Version`` travels with the prompts so the editor can send it back on save
    and have a concurrent save refused. The GraphQL field still answers with a
    JSON string, as it always has; what is in that string is now the prompts
    plus ``Version``, ``UpdatedBy`` and ``UpdatedAt``, rather than the whole
    DynamoDB item -- the partition key and the seeded guidance text the previous
    DynamoDB resolver returned are not in it. (The save side of the round trip
    takes a schema addition too: ``ExpectedVersion`` on the input.)
    """
    # Two identifiers, and nothing else: an unrecognised one is refused rather
    # than falling back to the baseline, which would answer a typo with a
    # plausible-looking record.
    if config_id == CUSTOM_CONFIG_ID:
        profile = OPERATOR_PROFILE
    elif config_id == DEFAULT_CONFIG_ID:
        profile = "default"
    else:
        raise ValueError(
            f"Unknown LLMPromptTemplateId {config_id!r}; expected "
            f"{DEFAULT_CONFIG_ID} or {CUSTOM_CONFIG_ID}"
        )
    record = CONFIG_STORE.read_profile(profile, consistent_read=True)
    prompts = _prompts_of(record.body)
    payload: Dict[str, Any] = dict(prompts)
    payload["Version"] = record.version
    if record.updated_by:
        payload["UpdatedBy"] = record.updated_by
    if record.updated_at:
        payload["UpdatedAt"] = record.updated_at
    print(
        f"Read summary prompt templates: id={config_id} profile={profile} "
        f"version={record.version} prompts={len(prompts)}"
    )
    return {"LLMPromptTemplateId": json.dumps(payload)}


def _write(input_data: Dict[str, Any], caller: Dict[str, Any]) -> Dict[str, Any]:
    """Serve the write: store the allowed fields as the profile's prompt section."""
    config_id = input_data["LLMPromptTemplateId"]
    template_config_str = input_data["TemplateConfig"]

    # Only the operator's profile is editable; the baseline is written by the
    # stack's configuration seeder and is what a reset falls back to.
    if config_id != CUSTOM_CONFIG_ID:
        raise ValueError(f"Only {CUSTOM_CONFIG_ID} can be updated")

    try:
        config_object = json.loads(template_config_str)
    except json.JSONDecodeError as error:
        raise ValueError(f"Invalid JSON in TemplateConfig: {error}") from error
    if not isinstance(config_object, dict):
        raise ValueError("TemplateConfig must be a JSON object")

    prompts = {}
    for key, value in config_object.items():
        if not TEMPLATE_FIELD_PATTERN.match(key):
            print(f"Filtered out non-template field: {key}")
            continue
        # A prompt is text. Refused here rather than stored, because the
        # summariser can only skip a value it cannot send to a model -- which
        # would make a save that was accepted turn into a section missing from
        # every later summary, with the error nowhere near the save.
        if not isinstance(value, str):
            raise ValueError(f"the prompt under {key!r} must be text; got {type(value).__name__}")
        prompts[key] = value

    expected_version = input_data.get("ExpectedVersion")
    if expected_version is not None:
        expected_version = int(expected_version)

    # One section, not the whole record: the profile is where every page's
    # settings will live, and replacing the body would delete the sections this
    # page knows nothing about.
    version = CONFIG_STORE.write_section(
        OPERATOR_PROFILE,
        SUMMARY_PROMPT_TEMPLATES.name,
        prompts,
        updated_by=caller["actor"],
        expected_version=expected_version,
    )
    print(
        f"Successfully updated the summary prompt templates: {len(prompts)} prompt(s), "
        f"version={version}"
    )
    return {"LLMPromptTemplateId": config_id, "Success": True, "Version": version}


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
    # Checked before the table is touched, and deliberately outside the catch-all
    # below so the refusal reaches the client as itself rather than as a save
    # failure. The AppSync resolver unit turns it into the error for this field.
    _require_admin(caller)
    field = _field_name(event)

    if field == QUERY_FIELD:
        try:
            return _read(event["arguments"]["LLMPromptTemplateId"])
        except Exception as error:
            print(f"Error reading LLM prompt template: {error}")
            raise Exception(f"Failed to read LLM prompt template: {error}") from error

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
        print(f"Error updating LLM prompt template: {error}")
        raise Exception(f"Failed to update LLM prompt template: {error}") from error
