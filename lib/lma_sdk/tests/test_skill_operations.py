# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
"""Unit tests for client.skills: the meeting assistant's skills."""

import base64
import json
from unittest.mock import MagicMock

import pytest

from lma_sdk.exceptions import LMAAppSyncError
from lma_sdk.operations import skills as skill_ops

SKILL = {
    "SkillId": "launch-review",
    "Name": "launch-review",
    "DisplayName": "Launch review",
    "Description": "Review a launch.",
    "Instructions": "Do it.",
    "Files": [{"Path": "notes.md", "Content": "n", "Size": 1}],
    "Metadata": json.dumps({"trigger": "review"}),
}
LIBRARY = {"kind": "lma-skill-library", "version": 1, "exportedAt": "x", "skills": []}


@pytest.fixture
def appsync(client):
    mock = MagicMock(name="appsync")
    client._appsync_ops = mock
    return mock


def _sent(appsync, call=-1):
    args = appsync.graphql.call_args_list[call]
    return args.args[0], args.kwargs.get("variables") or {}


def test_list_and_get_parse_the_metadata(client, appsync):
    appsync.graphql.return_value = {"listSkills": [SKILL]}
    assert client.skills.list()[0]["Metadata"] == {"trigger": "review"}
    query, _ = _sent(appsync)
    assert "listSkills" in query and "Instructions" not in query
    appsync.graphql.return_value = {"getSkill": SKILL}
    assert client.skills.get("launch-review")["Instructions"] == "Do it."
    assert _sent(appsync)[1] == {"skillId": "launch-review"}


def test_create_sends_files_and_metadata_and_raises_when_refused(client, appsync):
    appsync.graphql.return_value = {"createSkill": {"Success": True, "SkillId": "a", "Skill": SKILL}}
    result = client.skills.create(
        "launch-review", "d", "i", files={"notes.md": "n"}, metadata={"trigger": "t"}
    )
    assert result["Skill"]["Metadata"] == {"trigger": "review"}
    _, variables = _sent(appsync)
    assert variables["input"]["Files"] == [{"Path": "notes.md", "Content": "n"}]
    assert json.loads(variables["input"]["Metadata"]) == {"trigger": "t"}
    appsync.graphql.return_value = {"createSkill": {"Success": False, "Message": "A skill named x"}}
    with pytest.raises(LMAAppSyncError, match="A skill named x"):
        client.skills.create("x", "d", "i")


def test_update_keeps_every_part_it_is_not_given(client, appsync):
    appsync.graphql.side_effect = [
        {"getSkill": SKILL},
        {"updateSkill": {"Success": True, "SkillId": "launch-review", "Skill": SKILL}},
    ]
    client.skills.update("launch-review", description="New description.")
    _, variables = _sent(appsync)
    assert variables["skillId"] == "launch-review"
    sent = variables["input"]
    assert sent["Description"] == "New description."
    assert sent["Instructions"] == "Do it." and sent["Name"] == "launch-review"
    assert sent["Files"] == [{"Path": "notes.md", "Content": "n"}]
    assert json.loads(sent["Metadata"]) == {"trigger": "review"}


def test_update_of_a_missing_skill_raises(client, appsync):
    appsync.graphql.return_value = {"getSkill": None}
    with pytest.raises(LMAAppSyncError, match="no skill"):
        client.skills.update("nope", description="x")


def test_delete(client, appsync):
    appsync.graphql.return_value = {"deleteSkill": {"Success": True, "SkillId": "a"}}
    assert client.skills.delete("a")["Success"] is True
    assert _sent(appsync)[1] == {"skillId": "a"}


def test_import_file_sends_markdown_as_text_and_a_zip_as_base64(client, appsync, tmp_path):
    appsync.graphql.return_value = {"importSkill": {"Success": True, "Skipped": []}}
    md = tmp_path / "SKILL.md"
    md.write_text("---\nname: a\n---\n")
    client.skills.import_file(md)
    _, variables = _sent(appsync)
    assert variables["encoding"] == "utf-8" and variables["content"].startswith("---")
    assert variables["overwrite"] is False
    archive = tmp_path / "a.zip"
    archive.write_bytes(b"PK\x03\x04rest")
    client.skills.import_file(archive, overwrite=True)
    _, variables = _sent(appsync)
    assert variables["encoding"] == "base64"
    assert base64.b64decode(variables["content"]) == b"PK\x03\x04rest"
    assert variables["overwrite"] is True


def test_a_refused_import_raises_with_the_stacks_reason(client, appsync, tmp_path):
    appsync.graphql.return_value = {"importSkill": {"Success": False, "Message": "no front matter"}}
    md = tmp_path / "a.md"
    md.write_text("x")
    with pytest.raises(LMAAppSyncError, match="no front matter"):
        client.skills.import_file(md)


def test_export_writes_a_zip_as_bytes_and_skill_md_as_text(client, appsync, tmp_path):
    appsync.graphql.return_value = {
        "exportSkill": json.dumps(
            {"fileName": "a.zip", "encoding": "base64", "content": base64.b64encode(b"zipped").decode()}
        )
    }
    target = tmp_path / "a.zip"
    client.skills.export("a", target, file_format="zip")
    assert target.read_bytes() == b"zipped"
    assert _sent(appsync)[1] == {"skillId": "a", "format": "zip"}
    appsync.graphql.return_value = {
        "exportSkill": json.dumps({"fileName": "SKILL.md", "encoding": "utf-8", "content": "---\n"})
    }
    md = tmp_path / "SKILL.md"
    client.skills.export("a", md)
    assert md.read_text() == "---\n"


def test_export_library_checks_the_kind(client, appsync):
    appsync.graphql.return_value = {"exportSkillLibrary": json.dumps(LIBRARY)}
    assert client.skills.export_library() == LIBRARY
    appsync.graphql.return_value = {"exportSkillLibrary": json.dumps({"kind": "lma-mcp-server-library"})}
    with pytest.raises(LMAAppSyncError):
        client.skills.export_library()


@pytest.mark.parametrize(
    "document",
    [{"kind": "lma-mcp-server-library", "version": 1}, {**LIBRARY, "version": 2}, [], {**LIBRARY, "skills": None}],
    ids=["other-kind", "other-version", "not-an-object", "no-skills"],
)
def test_a_document_that_is_not_a_skill_library_is_refused_before_any_call(client, appsync, document):
    with pytest.raises(ValueError):
        client.skills.import_library(document)
    appsync.graphql.assert_not_called()


def test_a_large_library_is_sent_in_parts_of_whole_skills(client, appsync, monkeypatch):
    monkeypatch.setattr(skill_ops, "MAX_IMPORT_BYTES", 400)
    skills = [{"name": f"s{n}", "instructions": "x" * 150} for n in range(4)]
    appsync.graphql.side_effect = lambda *_a, **kw: {
        "importSkillLibrary": {
            "Success": True,
            "Skills": [
                {"Name": s["name"], "Action": "created"}
                for s in json.loads(kw["variables"]["library"])["skills"]
            ],
        }
    }
    result = client.skills.import_library({**LIBRARY, "skills": skills})
    assert appsync.graphql.call_count > 1
    assert [s["Name"] for s in result["Skills"]] == ["s0", "s1", "s2", "s3"]
    assert result["Success"] is True and result["Message"] == "4 of 4 skills processed"
    for call in appsync.graphql.call_args_list:
        part = json.loads(call.kwargs["variables"]["library"])
        assert part["kind"] == "lma-skill-library" and part["version"] == 1


def test_a_name_listed_twice_is_sent_once_and_reported_skipped(client, appsync, monkeypatch):
    monkeypatch.setattr(skill_ops, "MAX_IMPORT_BYTES", 200)
    skills = [{"name": "a", "instructions": "x" * 120}, {"name": "a", "instructions": "y" * 120}]
    appsync.graphql.side_effect = lambda *_a, **kw: {
        "importSkillLibrary": {
            "Success": True,
            "Skills": [
                {"Name": s["name"], "Action": "created"}
                for s in json.loads(kw["variables"]["library"])["skills"]
            ],
        }
    }
    result = client.skills.import_library({**LIBRARY, "skills": skills})
    assert appsync.graphql.call_count == 1
    assert [(s["Name"], s["Action"]) for s in result["Skills"]] == [
        ("a", "created"),
        ("a", "skipped"),
    ]


def test_a_library_refused_as_a_whole_reports_the_stacks_message(client, appsync):
    appsync.graphql.return_value = {
        "importSkillLibrary": {"Success": False, "Message": "too many", "Skills": []}
    }
    result = client.skills.import_library({**LIBRARY, "skills": [{"name": "a"}]})
    assert result == {"Success": False, "Skills": [], "Message": "too many"}
