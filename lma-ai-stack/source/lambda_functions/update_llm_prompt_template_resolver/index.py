"""
Lambda function resolver for updateLLMPromptTemplate mutation

Stores the custom summary prompt templates: the fields on the allowed list below,
plus the partition key and the audit attributes naming who saved them and when.
Membership of the Admin group is re-checked here as well as on the AppSync field.

Copyright (c) 2025 Amazon.com
This file is licensed under the MIT License.
"""

import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict

import boto3

dynamodb = boto3.resource("dynamodb")

CONFIG_ID = "CustomSummaryPromptTemplates"

ADMIN_GROUP = os.environ.get("ADMIN_GROUP", "Admin")

# Allowlist of valid prompt template fields, in "N#LABEL" form -- "1#SUMMARY",
# "2#DETAILS", "3#ACTIONS" and so on. The stored item holds exactly these plus
# the partition key and the audit attributes this resolver writes.
TEMPLATE_FIELD_PATTERN = re.compile(r"^\d+#")


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
            "the summary prompt templates"
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


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    AppSync Lambda resolver for updateLLMPromptTemplate

    Stores the custom summary prompt templates, keeping only the fields on the
    allowed list so the written item holds exactly the template configuration.

    Args:
        event: AppSync event with arguments and identity
        context: Lambda context

    Returns:
        Dict with LLMPromptTemplateId and Success status
    """
    caller = _get_caller_identity(event)
    # Checked before the table is touched, and deliberately outside the catch-all
    # below so the refusal reaches the client as itself rather than as a save
    # failure. The AppSync resolver unit turns it into the error for this field.
    _require_admin(caller)

    try:
        # Get table name from environment
        table_name = os.environ["LLM_PROMPT_TEMPLATE_TABLE_NAME"]
        table = dynamodb.Table(table_name)

        # Extract input from AppSync event
        input_data = event["arguments"]["input"]
        template_id = input_data["LLMPromptTemplateId"]
        template_config_str = input_data["TemplateConfig"]

        # Only the Custom record is editable; the Default one is seeded by the
        # llm-template-setup stack and is what a reset falls back to.
        if template_id != CONFIG_ID:
            raise ValueError(f"Only {CONFIG_ID} can be updated")

        # Parse the JSON configuration
        try:
            config_object = json.loads(template_config_str)
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON in TemplateConfig: {str(e)}")
        if not isinstance(config_object, dict):
            raise ValueError("TemplateConfig must be a JSON object")

        # Build item with only allowed fields
        item = {"LLMPromptTemplateId": template_id}

        for key, value in config_object.items():
            if TEMPLATE_FIELD_PATTERN.match(key):
                item[key] = value
            else:
                print(f"Filtered out non-template field: {key}")

        item.update(_audit_attributes(caller))

        # Store in DynamoDB
        table.put_item(Item=item)

        print(f"Successfully updated LLM prompt template: {template_id}")

        return {"LLMPromptTemplateId": template_id, "Success": True}

    except Exception as e:
        print(f"Error updating LLM prompt template: {str(e)}")
        raise Exception(f"Failed to update LLM prompt template: {str(e)}")
