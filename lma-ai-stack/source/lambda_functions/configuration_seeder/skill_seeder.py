#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""Seeds the meeting assistant's default skills into the skills table.

The skills a release ships are in ``default_skills/<name>/SKILL.md`` beside this
module, in the Agent Skills format the Skills page imports. Each is stored as an
ordinary skill, with ``SourceFormat: shipped``, which an Admin can edit or
delete like any other. See docs/meeting-assistant-skills.md#default-skills.

What a run may write, per default skill, is decided by a **ledger**: one record
per default skill in the same table, under the partition
``<account>#default-skills`` rather than the account's own, so no reader of the
skills (the Skills page, the library export, the meeting assistant) ever lists
it. The ledger record says which skill id the default was stored under and the
digest of what was stored. Then:

- **No ledger record, and no skill of the name**: the skill is created and the
  ledger record written (``created``). This is a new deployment, and an upgrade
  from a release that shipped no default skills.
- **No ledger record, and a skill of the name holding exactly the shipped
  text**: the ledger record is written for it (``adopted``). This is a run that
  stored the skill and failed before its ledger write, or a table restored
  without its ledger.
- **No ledger record, and a skill of the name holding other text**: an Admin's
  own skill. It is left untouched, nothing is recorded, and the default is not
  seeded (``name-in-use``); a later run seeds it if the name becomes free.
- **A ledger record, and no skill under its id**: an Admin deleted it. It stays
  deleted (``deleted``).
- **A ledger record, and the skill holds what was stored**: nobody changed it.
  When the release ships different text it is replaced (``updated``), keeping
  its id and creation fields; otherwise nothing is written (``unchanged``).
- **A ledger record, and the skill holds anything else**: an Admin edited it. It
  is left alone, whatever the release ships (``edited``).

Writes are conditional on what was read (``UpdatedAt`` for a replacement), so an
Admin's save landing between the read and the write wins, and the next run
decides again. Nothing here fails the deployment: a skill that cannot be seeded
is reported, and the next run that re-seeds tries again.

This module reads ``SKILL.md`` itself rather than importing the Skills Manager's
parser, which is in another function's package; the tests hold the two to the
same result for every shipped file.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional

import yaml
from botocore.exceptions import ClientError

LOGGER = logging.getLogger()

#: Where the shipped skills live inside this function's package.
DEFAULT_SKILLS_DIR = (
    Path(os.environ.get("LAMBDA_TASK_ROOT", Path(__file__).parent)) / "default_skills"
)
SKILL_FILE_NAME = "SKILL.md"

#: The SourceFormat a default skill is stored with; the Skills page shows it as
#: shipped with LMA, and the meeting assistant does not offer such a skill for
#: Participant use until the profile allows it.
SOURCE_SHIPPED = "shipped"

#: Who the audit fields name for what this writes, as the configuration profile
#: log names the seeder.
SEEDER_ACTOR = "cloudformation"

#: The ledger's partition is the account's with this suffix.
LEDGER_SUFFIX = "#default-skills"

#: The front-matter fields kept as a skill's metadata, as the Skills Manager
#: keeps them (skill_formats.METADATA_*).
METADATA_FIELDS = ("trigger", "icon", "license", "compatibility", "inputs", "metadata")

#: The Skills Manager's bound on how many skills a deployment stores
#: (skill_formats.MAX_SKILLS). A default is not seeded past it.
MAX_SKILLS = 40

_FRONT_MATTER = re.compile(r"\A\ufeff?---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.S)

#: The stored attributes that are the skill's content, as opposed to its keys,
#: source and audit fields.
CONTENT_FIELDS = ("Name", "DisplayName", "Description", "Instructions", "Files", "Metadata")


class DefaultSkill(NamedTuple):
    """One shipped skill: its name and the content fields it is stored with."""

    name: str
    fields: Dict[str, Any]


def read_skill_md(text: str) -> Dict[str, Any]:
    """The content fields of a shipped ``SKILL.md``, as the Skills Manager stores an import.

    Raises:
        ValueError: the file has no front matter, name, description or instructions.
    """
    match = _FRONT_MATTER.match(text)
    if not match:
        raise ValueError("a default skill starts with a '---' front-matter block")
    front = yaml.safe_load(match.group(1)) or {}
    body = text[match.end() :].lstrip("\r\n")
    name, description = front.get("name"), front.get("description")
    if not isinstance(name, str) or not name or not isinstance(description, str) or not description:
        raise ValueError("a default skill's front matter gives its name and description")
    if not body.strip():
        raise ValueError(f"the default skill {name} has no instructions")
    return {
        "Name": name,
        "DisplayName": str(front.get("display_name") or ""),
        "Description": description.strip(),
        "Instructions": body,
        "Files": [],
        "Metadata": {key: front[key] for key in METADATA_FIELDS if key in front},
    }


def shipped_default_skills(directory: Path = DEFAULT_SKILLS_DIR) -> List[DefaultSkill]:
    """Every skill this release ships, by name."""
    skills = []
    for path in sorted(directory.glob(f"*/{SKILL_FILE_NAME}")):
        fields = read_skill_md(path.read_text(encoding="utf-8"))
        if fields["Name"] != path.parent.name:
            raise ValueError(f"{path}: the skill's name must be its folder's name")
        skills.append(DefaultSkill(fields["Name"], fields))
    return skills


def content_digest(item: Optional[Dict[str, Any]]) -> str:
    """A digest of a skill's content fields, whatever else the record holds."""
    item = item or {}
    content = {
        "Name": item.get("Name"),
        "DisplayName": item.get("DisplayName") or "",
        "Description": item.get("Description") or "",
        "Instructions": item.get("Instructions") or "",
        "Files": [
            {"Path": f.get("Path"), "Content": f.get("Content") or ""}
            for f in item.get("Files") or []
        ],
        "Metadata": dict(item.get("Metadata") or {}),
    }
    encoded = json.dumps(content, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _conditional_failed(error: ClientError) -> bool:
    return error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


def _new_skill_id(name: str, taken: set) -> str:
    """The id a new skill of ``name`` gets, as the Skills Manager makes one."""
    if name not in taken:
        return name
    number = 2
    while f"{name}-{number}" in taken:
        number += 1
    return f"{name}-{number}"


class DefaultSkillSeeder:
    """Applies the rules in the module docstring to one skills table."""

    def __init__(self, table: Any, account_id: str) -> None:
        self.table = table
        self.account = account_id
        self.ledger_partition = f"{account_id}{LEDGER_SUFFIX}"

    def _query(self, partition: str) -> List[Dict[str, Any]]:
        kwargs: Dict[str, Any] = {
            "KeyConditionExpression": "AccountId = :account",
            "ExpressionAttributeValues": {":account": partition},
            "ConsistentRead": True,
        }
        items: List[Dict[str, Any]] = []
        while True:
            response = self.table.query(**kwargs)
            items.extend(response.get("Items", []))
            if "LastEvaluatedKey" not in response:
                return items
            kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]

    def _record(self, skill: DefaultSkill, skill_id: str, digest: str) -> None:
        self.table.put_item(
            Item={
                "AccountId": self.ledger_partition,
                "SkillId": skill.name,
                "StoredSkillId": skill_id,
                "ContentDigest": digest,
                "RecordedAt": _now(),
            }
        )

    def _item(self, skill: DefaultSkill, skill_id: str, existing: Optional[Dict[str, Any]]):
        now = _now()
        return {
            "AccountId": self.account,
            "SkillId": skill_id,
            **skill.fields,
            "SourceFormat": SOURCE_SHIPPED,
            "FileSizes": {},
            "CreatedAt": (existing or {}).get("CreatedAt") or now,
            "CreatedBy": (existing or {}).get("CreatedBy") or SEEDER_ACTOR,
            "UpdatedAt": now,
            "UpdatedBy": SEEDER_ACTOR,
        }

    def seed_one(
        self,
        skill: DefaultSkill,
        stored: List[Dict[str, Any]],
        ledger: Dict[str, Dict[str, Any]],
    ) -> str:
        """Seed one default skill; the outcome, as named in the module docstring."""
        shipped = content_digest(skill.fields)
        entry = ledger.get(skill.name)
        by_id = {str(s.get("SkillId")): s for s in stored}
        if entry is None:
            same_name = next((s for s in stored if s.get("Name") == skill.name), None)
            if same_name is not None:
                if content_digest(same_name) == shipped:
                    self._record(skill, str(same_name["SkillId"]), shipped)
                    return "adopted"
                LOGGER.warning(
                    "a skill named %s exists and is not the default skill of that name; "
                    "it is left as it is and the default skill is not seeded",
                    skill.name,
                )
                return "name-in-use"
            if len(stored) >= MAX_SKILLS:
                LOGGER.warning(
                    "the deployment holds %d skills, the most it can store; the default "
                    "skill %s is not seeded",
                    len(stored),
                    skill.name,
                )
                return "limit-reached"
            skill_id = _new_skill_id(skill.name, set(by_id))
            item = self._item(skill, skill_id, None)
            try:
                self.table.put_item(Item=item, ConditionExpression="attribute_not_exists(SkillId)")
            except ClientError as error:
                if _conditional_failed(error):
                    return "deferred"
                raise
            stored.append(item)
            self._record(skill, skill_id, shipped)
            return "created"

        skill_id = str(entry.get("StoredSkillId") or skill.name)
        current = by_id.get(skill_id)
        if current is None:
            return "deleted"
        if content_digest(current) != entry.get("ContentDigest"):
            return "edited"
        if entry.get("ContentDigest") == shipped:
            return "unchanged"
        try:
            self.table.put_item(
                Item=self._item(skill, skill_id, current),
                ConditionExpression="UpdatedAt = :seen",
                ExpressionAttributeValues={":seen": current.get("UpdatedAt")},
            )
        except ClientError as error:
            if _conditional_failed(error):
                # Saved or deleted between the read and the write: the next run
                # decides again.
                return "deferred"
            raise
        self._record(skill, skill_id, shipped)
        return "updated"

    def seed(self, skills: List[DefaultSkill]) -> Dict[str, str]:
        """Seed every default skill; ``name -> outcome``."""
        stored = self._query(self.account)
        ledger = {str(e["SkillId"]): e for e in self._query(self.ledger_partition)}
        outcomes: Dict[str, str] = {}
        for skill in skills:
            try:
                outcomes[skill.name] = self.seed_one(skill, stored, ledger)
            except ClientError:
                LOGGER.warning("could not seed the default skill %s now", skill.name, exc_info=True)
                outcomes[skill.name] = "deferred"
            LOGGER.info("default skill %s: %s", skill.name, outcomes[skill.name])
        return outcomes


def seed_default_skills(
    table: Any, account_id: str, skills: Optional[List[DefaultSkill]] = None
) -> str:
    """Seed the shipped skills into ``table``; reported as ``name=outcome`` pairs."""
    chosen = shipped_default_skills() if skills is None else skills
    outcomes = DefaultSkillSeeder(table, account_id).seed(chosen)
    return ",".join(f"{name}={outcome}" for name, outcome in sorted(outcomes.items())) or "none"
