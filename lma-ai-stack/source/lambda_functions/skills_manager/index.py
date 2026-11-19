#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""Skills Manager: the meeting assistant's skills library, behind AppSync.

Skills are account-wide, as installed MCP servers are, and are stored in their
own table (SKILLS_TABLE, keyed by AccountId and SkillId). See skill_formats.py
for what a skill holds and the files it is read from and written to.

Who may call what, at two independent layers (the AppSync schema's directives
and the checks here):

- The reads (``listSkills``, ``getSkill``, ``exportSkill``) are for every
  signed-in user, as ``listInstalledMCPServers`` is: the skills shape the
  answers every user of the meeting assistant gets, and they hold no
  credential.
- Every write, and the export of the whole library, is for the Admin group and
  for IAM-signed callers (the LMA SDK and CLI). This handler re-checks the Admin
  group for a user-pool caller.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import boto3
import skill_formats
from botocore.config import Config as BotoCoreConfig
from botocore.exceptions import ClientError

LOGGER = logging.getLogger()
LOGGER.setLevel(logging.INFO)

CLIENT_CONFIG = BotoCoreConfig(retries={"mode": "adaptive", "max_attempts": 3})
DYNAMODB = boto3.resource("dynamodb", config=CLIENT_CONFIG)

SKILLS_TABLE = os.environ.get("SKILLS_TABLE", "")
ACCOUNT_ID = os.environ.get("AWS_ACCOUNT_ID", "")
ADMIN_GROUP = os.environ.get("ADMIN_GROUP", "Admin")

READ_FIELDS = frozenset({"listSkills", "getSkill", "exportSkill"})
#: For the Admin group and for IAM-signed callers.
ADMIN_FIELDS = frozenset(
    {
        "createSkill",
        "updateSkill",
        "deleteSkill",
        "importSkill",
        "importSkillLibrary",
        "exportSkillLibrary",
    }
)

EXPORT_FORMATS = ("skill-md", "zip")


class ForbiddenError(Exception):
    """The caller may not perform the operation."""


# ── callers ─────────────────────────────────────────────────────────────────


def _is_iam_caller(event: Dict[str, Any]) -> bool:
    """IAM-signed: AppSync gives the signing principal's ARN and no user-pool claims."""
    identity = event.get("identity") or {}
    return bool(identity.get("userArn")) and not identity.get("claims")


def _caller(event: Dict[str, Any]) -> Dict[str, Any]:
    identity = event.get("identity") or {}
    claims = identity.get("claims") or {}
    groups = claims.get("cognito:groups") or identity.get("groups") or []
    if isinstance(groups, str):
        groups = [g.strip() for g in groups.split(",") if g.strip()]
    elif not isinstance(groups, (list, tuple)):
        groups = []
    if _is_iam_caller(event):
        who = str(identity.get("userArn"))
    else:
        who = str(
            claims.get("email")
            or claims.get("cognito:username")
            or identity.get("username")
            or claims.get("sub")
            or ""
        )
    return {"who": who, "is_admin": ADMIN_GROUP in groups}


def authorize(event: Dict[str, Any], field_name: str) -> None:
    """Raise ForbiddenError unless the caller may call ``field_name``."""
    if field_name in READ_FIELDS:
        return
    if field_name in ADMIN_FIELDS:
        if _is_iam_caller(event) or _caller(event)["is_admin"]:
            return
        raise ForbiddenError(f"Only members of the {ADMIN_GROUP} group can manage skills")
    raise ForbiddenError(f"Unknown operation: {field_name}")


# ── storage ─────────────────────────────────────────────────────────────────


def _account() -> str:
    return ACCOUNT_ID or os.environ.get("AWS_ACCOUNT_ID", "unknown")


def _table() -> Any:
    return DYNAMODB.Table(SKILLS_TABLE)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _stored(projection: bool = False) -> List[Dict[str, Any]]:
    """Every stored skill of the account; with ``projection``, without instructions or files."""
    kwargs: Dict[str, Any] = {
        "KeyConditionExpression": "AccountId = :account",
        "ExpressionAttributeValues": {":account": _account()},
    }
    if projection:
        kwargs["ProjectionExpression"] = (
            "SkillId, #name, DisplayName, Description, Metadata, SourceFormat, "
            "CreatedAt, CreatedBy, UpdatedAt, UpdatedBy, FileSizes"
        )
        kwargs["ExpressionAttributeNames"] = {"#name": "Name"}
    items: List[Dict[str, Any]] = []
    while True:
        response = _table().query(**kwargs)
        items.extend(response.get("Items", []))
        if "LastEvaluatedKey" not in response:
            return items
        kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]


def _get(skill_id: str) -> Optional[Dict[str, Any]]:
    return _table().get_item(Key={"AccountId": _account(), "SkillId": skill_id}).get("Item")


def _file_sizes(skill: skill_formats.Skill) -> Dict[str, int]:
    return {f.path: len(f.content.encode("utf-8")) for f in skill.files}


def _put(skill_id: str, skill: skill_formats.Skill, who: str, existing: Optional[Dict]) -> None:
    now = _now()
    item = {
        "AccountId": _account(),
        "SkillId": skill_id,
        **skill.to_item_fields(),
        # The sizes of the reference files, so the list can show them without
        # reading every file's content.
        "FileSizes": _file_sizes(skill),
        "CreatedAt": (existing or {}).get("CreatedAt") or now,
        "CreatedBy": (existing or {}).get("CreatedBy") or who,
        "UpdatedAt": now,
        "UpdatedBy": who,
    }
    if existing is None:
        _table().put_item(Item=item, ConditionExpression="attribute_not_exists(SkillId)")
    else:
        _table().put_item(Item=item, ConditionExpression="attribute_exists(SkillId)")


# ── the GraphQL shape ───────────────────────────────────────────────────────


def to_graphql(item: Optional[Dict[str, Any]], summary: bool = False) -> Optional[Dict[str, Any]]:
    """A stored skill as the GraphQL ``Skill`` type; ``summary`` leaves out its text."""
    if not item:
        return None
    sizes = item.get("FileSizes") or {}
    if summary:
        files = [{"Path": path, "Size": int(size)} for path, size in sorted(sizes.items())]
    else:
        files = [
            {
                "Path": f.get("Path"),
                "Content": f.get("Content") or "",
                "Size": len(str(f.get("Content") or "").encode("utf-8")),
            }
            for f in item.get("Files") or []
        ]
    shaped = {
        "SkillId": item.get("SkillId"),
        "Name": item.get("Name"),
        "DisplayName": item.get("DisplayName") or None,
        "Description": item.get("Description") or "",
        "Files": files,
        "Metadata": json.dumps(item.get("Metadata") or {}, default=str),
        "SourceFormat": item.get("SourceFormat") or None,
        "CreatedAt": item.get("CreatedAt"),
        "CreatedBy": item.get("CreatedBy"),
        "UpdatedAt": item.get("UpdatedAt"),
        "UpdatedBy": item.get("UpdatedBy"),
    }
    if not summary:
        shaped["Instructions"] = item.get("Instructions") or ""
    return shaped


def _output(success: bool, message: str, skill_id: str = "", item: Optional[Dict] = None) -> Dict:
    return {
        "SkillId": skill_id or None,
        "Success": success,
        "Message": message,
        "Skill": to_graphql(item),
    }


def _skill_from_input(data: Any, source_format: str = skill_formats.SOURCE_MANUAL):
    if not isinstance(data, dict):
        raise skill_formats.SkillError("The skill is missing.")
    return skill_formats.check_skill(
        data.get("Name"),
        data.get("Description"),
        data.get("Instructions"),
        display_name=data.get("DisplayName"),
        files=data.get("Files") or [],
        metadata=data.get("Metadata"),
        source_format=source_format,
    )


def _name_taken(name: str, other_than: str = "") -> bool:
    return any(s.get("Name") == name and s.get("SkillId") != other_than for s in _stored(True))


# ── operations ──────────────────────────────────────────────────────────────


def list_skills(_event: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Every skill, without its instructions or file contents, sorted by name."""
    items = sorted(_stored(projection=True), key=lambda i: str(i.get("Name") or ""))
    return [to_graphql(i, summary=True) for i in items]


def get_skill(event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    skill_id = (event.get("arguments") or {}).get("skillId")
    return to_graphql(_get(skill_id)) if skill_id else None


def export_skill(event: Dict[str, Any]) -> str:
    """One skill as a file: SKILL.md text, or a zip of its folder in base64."""
    arguments = event.get("arguments") or {}
    file_format = arguments.get("format") or "skill-md"
    if file_format not in EXPORT_FORMATS:
        raise ValueError(f"format must be one of {', '.join(EXPORT_FORMATS)}")
    item = _get(str(arguments.get("skillId") or ""))
    if not item:
        raise ValueError("Skill not found")
    if file_format == "zip":
        content = base64.b64encode(skill_formats.render_skill_zip(item)).decode("ascii")
        answer = {"fileName": f"{item['Name']}.zip", "encoding": "base64", "content": content}
    else:
        answer = {
            "fileName": skill_formats.SKILL_FILE_NAME,
            "encoding": "utf-8",
            "content": skill_formats.render_skill_md(item),
        }
    return json.dumps({"format": file_format, **answer})


def export_skill_library(_event: Dict[str, Any]) -> str:
    document = skill_formats.export_library(_stored())
    LOGGER.info("Exported a skill library of %s skills", len(document["skills"]))
    return json.dumps(document, default=str)


def create_skill(event: Dict[str, Any]) -> Dict[str, Any]:
    try:
        skill = _skill_from_input((event.get("arguments") or {}).get("input"))
    except skill_formats.SkillError as exc:
        return _output(False, str(exc))
    stored = _stored(projection=True)
    if any(s.get("Name") == skill.name for s in stored):
        return _output(False, f"A skill named {skill.name} already exists.")
    if len(stored) >= skill_formats.MAX_SKILLS:
        return _output(False, f"At most {skill_formats.MAX_SKILLS} skills can be stored.")
    skill_id = skill_formats.new_skill_id(skill.name, {str(s.get("SkillId")) for s in stored})
    _put(skill_id, skill, _caller(event)["who"], None)
    LOGGER.info("Created skill %s", skill_id)
    return _output(True, "Skill created", skill_id, _get(skill_id))


def update_skill(event: Dict[str, Any]) -> Dict[str, Any]:
    arguments = event.get("arguments") or {}
    skill_id = str(arguments.get("skillId") or "")
    existing = _get(skill_id) if skill_id else None
    if not existing:
        return _output(False, "Skill not found", skill_id)
    try:
        skill = _skill_from_input(
            arguments.get("input"), existing.get("SourceFormat") or skill_formats.SOURCE_MANUAL
        )
    except skill_formats.SkillError as exc:
        return _output(False, str(exc), skill_id)
    if _name_taken(skill.name, other_than=skill_id):
        return _output(False, f"Another skill is named {skill.name}.", skill_id)
    _put(skill_id, skill, _caller(event)["who"], existing)
    LOGGER.info("Updated skill %s", skill_id)
    return _output(True, "Skill updated", skill_id, _get(skill_id))


def delete_skill(event: Dict[str, Any]) -> Dict[str, Any]:
    skill_id = str((event.get("arguments") or {}).get("skillId") or "")
    if not skill_id:
        return _output(False, "skillId is required")
    try:
        _table().delete_item(
            Key={"AccountId": _account(), "SkillId": skill_id},
            ConditionExpression="attribute_exists(SkillId)",
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return _output(False, "Skill not found", skill_id)
        raise
    LOGGER.info("Deleted skill %s", skill_id)
    return _output(True, "Skill deleted", skill_id)


def _file_bytes(content: Any, encoding: str) -> bytes:
    if not isinstance(content, str):
        raise skill_formats.SkillError("The file content is missing.")
    if encoding == "base64":
        try:
            return base64.b64decode(content, validate=True)
        except (binascii.Error, ValueError):
            raise skill_formats.SkillError("The file content is not valid base64.") from None
    if encoding in ("", "utf-8"):
        return content.encode("utf-8")
    raise skill_formats.SkillError("encoding must be utf-8 or base64")


def import_skill(event: Dict[str, Any]) -> Dict[str, Any]:
    """One skill from a SKILL.md, an Amazon Quick skill .md, or a zip of a skill folder."""
    arguments = event.get("arguments") or {}
    file_name = str(arguments.get("fileName") or "")
    overwrite = arguments.get("overwrite") is True

    def failed(message: str) -> Dict[str, Any]:
        return {
            "Success": False,
            "Message": message,
            "SkillId": None,
            "Name": None,
            "Action": "failed",
            "Skipped": [],
            "Notes": [],
        }

    try:
        data = _file_bytes(arguments.get("content"), str(arguments.get("encoding") or ""))
        parsed = skill_formats.parse_skill_file(file_name, data)
    except skill_formats.SkillError as exc:
        return failed(str(exc))
    skill = parsed.skill
    stored = _stored(projection=True)
    existing_summary = next((s for s in stored if s.get("Name") == skill.name), None)
    if existing_summary is not None and not overwrite:
        return failed(
            f"A skill named {skill.name} already exists; import it again with replace chosen "
            "to replace it."
        )
    if existing_summary is None and len(stored) >= skill_formats.MAX_SKILLS:
        return failed(f"At most {skill_formats.MAX_SKILLS} skills can be stored.")
    if existing_summary is not None:
        skill_id = str(existing_summary["SkillId"])
        _put(skill_id, skill, _caller(event)["who"], _get(skill_id))
        action = "updated"
    else:
        skill_id = skill_formats.new_skill_id(skill.name, {str(s.get("SkillId")) for s in stored})
        _put(skill_id, skill, _caller(event)["who"], None)
        action = "created"
    LOGGER.info("Imported skill %s (%s, %s files left out)", skill_id, action, len(parsed.skipped))
    skipped = [{"Path": s.path, "Reason": s.reason} for s in parsed.skipped]
    message = f"Skill {skill.name} {action}"
    if skipped:
        message += f"; {len(skipped)} file{'s' if len(skipped) != 1 else ''} left out"
    return {
        "Success": True,
        "Message": message,
        "SkillId": skill_id,
        "Name": skill.name,
        "Action": action,
        "Skipped": skipped,
        "Notes": parsed.notes,
    }


def import_skill_library(event: Dict[str, Any]) -> Dict[str, Any]:
    """Create or update the skills a library document lists, matched by name."""
    library = (event.get("arguments") or {}).get("library")
    try:
        steps = skill_formats.plan_import(library, _stored())
    except skill_formats.SkillError as exc:
        return {"Success": False, "Message": str(exc), "Skills": []}
    who = _caller(event)["who"]
    results = []
    for step in steps:
        result = {"Name": step.name, "SkillId": step.skill_id or None, "Message": step.message}
        if step.action == "skip":
            results.append({**result, "Action": "skipped"})
            continue
        try:
            existing = _get(step.skill_id) if step.action == "update" else None
            _put(step.skill_id, step.skill, who, existing)
        except ClientError as exc:
            LOGGER.error("Skill %s of the library could not be stored", step.skill_id)
            results.append({**result, "Action": "failed", "Message": str(exc)})
            continue
        left_out = ", ".join(f"{s.path} ({s.reason})" for s in step.skipped)
        results.append(
            {
                **result,
                "Action": "created" if step.action == "create" else "updated",
                "Message": f"left out: {left_out}" if left_out else "",
            }
        )
    failed = [r for r in results if r["Action"] == "failed"]
    LOGGER.info("Imported a skill library: %s", {r["Name"]: r["Action"] for r in results})
    return {
        "Success": not failed,
        "Message": f"{len(results) - len(failed)} of {len(results)} skills processed",
        "Skills": results,
    }


OPERATIONS = {
    "listSkills": list_skills,
    "getSkill": get_skill,
    "exportSkill": export_skill,
    "exportSkillLibrary": export_skill_library,
    "createSkill": create_skill,
    "updateSkill": update_skill,
    "deleteSkill": delete_skill,
    "importSkill": import_skill,
    "importSkillLibrary": import_skill_library,
}


def handler(event: Dict[str, Any], context: Any) -> Any:  # pylint: disable=unused-argument
    """Route an AppSync field to its operation, after checking the caller may call it."""
    field_name = (event.get("info") or {}).get("fieldName", "")
    LOGGER.info("Skills Manager - Field: %s", field_name)
    try:
        authorize(event, field_name)
    except ForbiddenError as exc:
        raise Exception(f"Unauthorized: {exc}") from exc  # pylint: disable=broad-exception-raised
    if not SKILLS_TABLE:
        raise Exception("The skills table is not configured")  # pylint: disable=broad-exception-raised
    return OPERATIONS[field_name](event)
