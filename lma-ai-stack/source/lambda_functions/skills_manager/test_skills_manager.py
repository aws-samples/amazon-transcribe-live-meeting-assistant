#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""The Skills Manager resolver, against an in-memory table (no AWS calls)."""

from __future__ import annotations

import base64
import io
import json
import os
import sys
import zipfile
from pathlib import Path
from unittest import mock

import pytest
from botocore.exceptions import ClientError

os.environ.setdefault("SKILLS_TABLE", "skills")
os.environ.setdefault("AWS_ACCOUNT_ID", "111122223333")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

sys.path.insert(0, str(Path(__file__).parent))

with mock.patch("boto3.resource"):
    import index  # noqa: E402

ADMIN = {"claims": {"cognito:groups": ["Admin"], "email": "admin@example.com"}}
USER = {"claims": {"cognito:groups": [], "email": "user@example.com"}}
IAM = {"userArn": "the-signing-principal"}

SKILL = {
    "Name": "launch-review",
    "DisplayName": "Launch review",
    "Description": "Review a launch plan against the checklist.",
    "Instructions": "# Launch review\n\nRead reference/checklist.md and compare.",
    "Files": [{"Path": "reference/checklist.md", "Content": "- Pricing\n"}],
    "Metadata": json.dumps({"trigger": "review the launch"}),
}


class _Table:
    """The DynamoDB table calls the resolver makes, on a dict."""

    def __init__(self):
        self.items: dict = {}

    def query(self, **kwargs):
        account = kwargs["ExpressionAttributeValues"][":account"]
        items = [dict(v) for (a, _), v in sorted(self.items.items()) if a == account]
        if "ProjectionExpression" in kwargs:
            aliases = kwargs["ExpressionAttributeNames"]
            kept = {aliases[a.strip()] for a in kwargs["ProjectionExpression"].split(",")}
            assert "Instructions" not in kept and "Files" not in kept
            items = [{k: v for k, v in i.items() if k in kept} for i in items]
        return {"Items": items}

    def get_item(self, Key):
        item = self.items.get((Key["AccountId"], Key["SkillId"]))
        return {"Item": dict(item)} if item else {}

    def _check(self, key, condition):
        exists = key in self.items
        if (condition == "attribute_not_exists(SkillId)" and exists) or (
            condition == "attribute_exists(SkillId)" and not exists
        ):
            raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "op")

    def put_item(self, Item, ConditionExpression):
        key = (Item["AccountId"], Item["SkillId"])
        self._check(key, ConditionExpression)
        self.items[key] = dict(Item)

    def delete_item(self, Key, ConditionExpression):
        key = (Key["AccountId"], Key["SkillId"])
        self._check(key, ConditionExpression)
        del self.items[key]


@pytest.fixture
def table(monkeypatch):
    fake = _Table()
    monkeypatch.setattr(index, "DYNAMODB", mock.Mock(Table=lambda _name: fake))
    monkeypatch.setattr(index, "SKILLS_TABLE", "skills")
    monkeypatch.setattr(index, "ACCOUNT_ID", "111122223333")
    return fake


def call(field, identity=ADMIN, **arguments):
    return index.handler(
        {"info": {"fieldName": field}, "identity": identity, "arguments": arguments}, None
    )


# ── who may call what ──────────────────────────────────────────────────────

WRITES = [
    ("createSkill", {"input": SKILL}),
    ("updateSkill", {"skillId": "launch-review", "input": SKILL}),
    ("deleteSkill", {"skillId": "launch-review"}),
    ("importSkill", {"fileName": "SKILL.md", "content": "x"}),
    ("importSkillLibrary", {"library": "{}"}),
    ("exportSkillLibrary", {}),
]


@pytest.mark.parametrize("field,arguments", WRITES, ids=[w[0] for w in WRITES])
def test_a_signed_in_user_outside_the_admin_group_cannot_write(table, field, arguments):
    with pytest.raises(Exception, match="Unauthorized"):
        call(field, USER, **arguments)
    assert table.items == {}


@pytest.mark.parametrize("identity", [ADMIN, IAM], ids=["admin", "iam"])
def test_an_admin_and_an_iam_caller_can_create(table, identity):
    result = call("createSkill", identity, input=SKILL)
    assert result["Success"] is True, result
    stored = table.items[("111122223333", "launch-review")]
    assert stored["CreatedBy"] == ("admin@example.com" if identity is ADMIN else IAM["userArn"])


def test_every_signed_in_user_can_read(table):
    call("createSkill", input=SKILL)
    assert [s["Name"] for s in call("listSkills", USER)] == ["launch-review"]
    assert call("getSkill", USER, skillId="launch-review")["Instructions"] == SKILL["Instructions"]
    assert json.loads(call("exportSkill", USER, skillId="launch-review"))["fileName"] == "SKILL.md"


def test_a_user_pool_token_with_a_user_arn_is_not_taken_for_an_iam_caller(table):
    with pytest.raises(Exception, match="Unauthorized"):
        call("createSkill", {**USER, "userArn": "a-signing-principal"}, input=SKILL)


def test_an_unknown_field_is_refused(table):
    with pytest.raises(Exception, match="Unauthorized"):
        call("dropTable")


# ── CRUD ───────────────────────────────────────────────────────────────────


def test_create_stores_the_skill_with_who_and_when(table):
    result = call("createSkill", input=SKILL)
    assert result["SkillId"] == "launch-review"
    skill = result["Skill"]
    assert skill["Files"] == [
        {"Path": "reference/checklist.md", "Content": "- Pricing\n", "Size": 10}
    ]
    assert json.loads(skill["Metadata"]) == {"trigger": "review the launch"}
    assert skill["CreatedAt"] == skill["UpdatedAt"]
    assert skill["SourceFormat"] == "manual"


def test_the_list_leaves_out_instructions_and_file_contents(table):
    call("createSkill", input=SKILL)
    (listed,) = call("listSkills")
    assert "Instructions" not in listed
    assert listed["Files"] == [{"Path": "reference/checklist.md", "Size": 10}]


def test_a_second_skill_of_the_same_name_is_refused(table):
    call("createSkill", input=SKILL)
    result = call("createSkill", input=SKILL)
    assert result["Success"] is False and "already exists" in result["Message"]


def test_a_create_naming_a_script_is_refused_and_stores_nothing(table):
    bad = {**SKILL, "Files": [{"Path": "run.sh", "Content": "echo"}]}
    result = call("createSkill", input=bad)
    assert result["Success"] is False and "only text and Markdown" in result["Message"]
    assert table.items == {}


def test_update_keeps_the_id_and_creation_and_records_the_editor(table):
    call("createSkill", input=SKILL)
    result = call(
        "updateSkill", IAM, skillId="launch-review", input={**SKILL, "Name": "launch-check"}
    )
    assert result["Success"] is True, result
    stored = table.items[("111122223333", "launch-review")]
    assert stored["Name"] == "launch-check"
    assert stored["CreatedBy"] == "admin@example.com"
    assert stored["UpdatedBy"] == IAM["userArn"]


def test_update_of_a_missing_skill_or_to_a_taken_name_is_refused(table):
    assert call("updateSkill", skillId="nope", input=SKILL)["Message"] == "Skill not found"
    call("createSkill", input=SKILL)
    call("createSkill", input={**SKILL, "Name": "other"})
    result = call("updateSkill", skillId="other", input=SKILL)
    assert result["Success"] is False and "Another skill" in result["Message"]


def test_delete_removes_the_skill_and_reports_a_missing_one(table):
    call("createSkill", input=SKILL)
    assert call("deleteSkill", skillId="launch-review")["Success"] is True
    assert table.items == {}
    assert call("deleteSkill", skillId="launch-review")["Message"] == "Skill not found"


def test_create_stops_at_the_skill_limit(table, monkeypatch):
    monkeypatch.setattr(index.skill_formats, "MAX_SKILLS", 1)
    call("createSkill", input=SKILL)
    result = call("createSkill", input={**SKILL, "Name": "second"})
    assert result["Success"] is False and "At most 1" in result["Message"]


# ── single-skill import and export ─────────────────────────────────────────


def _zip(entries):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return base64.b64encode(buffer.getvalue()).decode()


SKILL_MD = "---\nname: launch-review\ndescription: Review a launch.\n---\n\nCheck it.\n"


def test_a_zip_import_stores_the_skill_and_reports_what_it_left_out(table):
    content = _zip(
        {
            "launch-review/SKILL.md": SKILL_MD,
            "launch-review/notes.md": "n",
            "launch-review/scripts/run.py": "print(1)",
        }
    )
    result = call("importSkill", fileName="launch-review.zip", content=content, encoding="base64")
    assert result["Success"] is True, result
    assert result["Action"] == "created"
    assert [s["Path"] for s in result["Skipped"]] == ["scripts/run.py"]
    assert "1 file left out" in result["Message"]
    stored = table.items[("111122223333", "launch-review")]
    assert [f["Path"] for f in stored["Files"]] == ["notes.md"]
    assert stored["SourceFormat"] == "agent-skills"


def test_an_import_over_an_existing_name_needs_replace(table):
    call("importSkill", fileName="SKILL.md", content=SKILL_MD)
    refused = call("importSkill", fileName="SKILL.md", content=SKILL_MD)
    assert refused["Success"] is False and "replace" in refused["Message"]
    replaced = call("importSkill", fileName="SKILL.md", content=SKILL_MD, overwrite=True)
    assert replaced["Action"] == "updated" and replaced["SkillId"] == "launch-review"


def test_an_import_of_bad_base64_or_a_bad_file_fails_cleanly(table):
    assert (
        "base64"
        in call("importSkill", fileName="a.zip", content="%%%", encoding="base64")["Message"]
    )
    assert call("importSkill", fileName="a.md", content="no front matter")["Success"] is False
    assert table.items == {}


def test_export_as_skill_md_and_as_zip(table):
    call("createSkill", input=SKILL)
    md = json.loads(call("exportSkill", skillId="launch-review", format="skill-md"))
    assert md["encoding"] == "utf-8" and md["content"].startswith("---\nname: launch-review")
    packed = json.loads(call("exportSkill", skillId="launch-review", format="zip"))
    assert packed["fileName"] == "launch-review.zip" and packed["encoding"] == "base64"
    with zipfile.ZipFile(io.BytesIO(base64.b64decode(packed["content"]))) as archive:
        assert "launch-review/reference/checklist.md" in archive.namelist()
    with pytest.raises(ValueError):
        call("exportSkill", skillId="launch-review", format="tar")
    with pytest.raises(ValueError):
        call("exportSkill", skillId="missing")


# ── the library ────────────────────────────────────────────────────────────


def test_a_library_round_trip_updates_every_skill_and_changes_none(table):
    call("createSkill", input=SKILL)
    call("createSkill", input={**SKILL, "Name": "second", "Files": []})
    before = json.loads(call("exportSkillLibrary", IAM))
    result = call("importSkillLibrary", IAM, library=json.dumps(before))
    assert result["Success"] is True
    assert {(s["Name"], s["Action"]) for s in result["Skills"]} == {
        ("launch-review", "updated"),
        ("second", "updated"),
    }
    after = json.loads(call("exportSkillLibrary", IAM))
    assert after["skills"] == before["skills"]


def test_a_library_import_into_another_deployment_creates_the_skills(table):
    call("createSkill", input=SKILL)
    document = call("exportSkillLibrary")
    table.items.clear()
    result = call("importSkillLibrary", library=document)
    assert [(s["SkillId"], s["Action"]) for s in result["Skills"]] == [("launch-review", "created")]
    assert table.items[("111122223333", "launch-review")]["SourceFormat"] == "library"


def test_a_library_that_is_not_one_is_refused(table):
    result = call("importSkillLibrary", library=json.dumps({"kind": "lma-mcp-server-library"}))
    assert result == {
        "Success": False,
        "Message": "The file is not a skill library (kind 'lma-skill-library').",
        "Skills": [],
    }
