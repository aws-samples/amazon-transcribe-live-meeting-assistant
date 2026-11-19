#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""The skill file formats and the skill library (skill_formats.py; no AWS)."""

from __future__ import annotations

import io
import json
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import skill_formats as sf  # noqa: E402

REPO = Path(__file__).resolve().parents[4]
QUICK_PACK = REPO / "amazon-quick-desktop-skills-pack" / "skills"

SKILL_MD = """---
name: launch-review
description: Review a product launch plan against the launch checklist. Use when asked to review a launch.
license: MIT-0
metadata:
  author: product-team
  version: 1.0
---

# Launch review

1. Read the checklist in reference/checklist.md.
2. Compare the plan discussed in the meeting with it.
"""


def _zip(entries: dict, modes: dict | None = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in entries.items():
            info = zipfile.ZipInfo(name)
            info.external_attr = ((modes or {}).get(name, 0o644)) << 16
            archive.writestr(info, content)
    return buffer.getvalue()


# ── SKILL.md ───────────────────────────────────────────────────────────────


def test_a_skill_md_is_read_from_its_front_matter_and_body():
    parsed = sf.parse_skill_file("SKILL.md", SKILL_MD.encode())
    skill = parsed.skill
    assert skill.name == "launch-review"
    assert skill.description.startswith("Review a product launch plan")
    assert skill.instructions.startswith("# Launch review")
    assert skill.metadata == {
        "license": "MIT-0",
        "metadata": {"author": "product-team", "version": "1.0"},
    }
    assert skill.source_format == sf.SOURCE_AGENT_SKILLS
    assert parsed.skipped == []


def test_a_skill_md_without_front_matter_is_refused():
    with pytest.raises(sf.SkillError, match="no front matter"):
        sf.parse_skill_file("SKILL.md", b"# Just a heading\n\nSome text.")


def test_a_skill_md_without_a_description_is_refused():
    with pytest.raises(sf.SkillError, match="description"):
        sf.parse_skill_file("SKILL.md", b"---\nname: x\n---\nBody")


def test_front_matter_that_is_not_yaml_is_refused():
    with pytest.raises(sf.SkillError, match="not valid YAML"):
        sf.parse_skill_file("SKILL.md", b"---\nname: [unclosed\n---\nBody")


def test_a_name_that_is_not_a_skill_name_is_stored_as_one_and_said_so():
    parsed = sf.parse_skill_file(
        "SKILL.md", b"---\nname: Launch Review!\ndescription: d\n---\nBody\n"
    )
    assert parsed.skill.name == "launch-review"
    assert any("stored as 'launch-review'" in note for note in parsed.notes)


def test_allowed_tools_is_not_kept_and_the_import_says_why():
    parsed = sf.parse_skill_file(
        "SKILL.md", b"---\nname: a\ndescription: d\nallowed-tools: Bash(git:*)\n---\nBody\n"
    )
    assert "allowed-tools" not in parsed.skill.metadata
    assert any("allowed-tools" in note for note in parsed.notes)


# ── Amazon Quick ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("path", sorted(QUICK_PACK.glob("*.md")), ids=lambda p: p.name)
def test_every_skill_of_the_quick_pack_imports(path):
    parsed = sf.parse_skill_file(path.name, path.read_bytes())
    skill = parsed.skill
    assert skill.source_format == sf.SOURCE_QUICK
    assert skill.name == path.stem
    assert skill.display_name
    assert skill.metadata["trigger"]
    assert skill.metadata["inputs"]
    assert "Trigger phrase:" in skill.description
    assert skill.instructions.lstrip().startswith("#")


def test_a_quick_skill_takes_its_description_from_its_first_paragraph():
    text = (
        "---\nname: lma-meeting-catchup\ndisplay_name: LMA Meeting Catch-up\n"
        "trigger: catch me up on\nicon: 📋\ninputs:\n  - meeting_description\n---\n\n"
        "# LMA Meeting Catch-up\n\nQuickly catch up on meetings you missed.\n\n## Workflow\n"
    )
    skill = sf.parse_skill_file("catchup.md", text.encode()).skill
    assert skill.description == (
        'Quickly catch up on meetings you missed. Trigger phrase: "catch me up on".'
    )
    assert skill.display_name == "LMA Meeting Catch-up"
    assert skill.metadata == {
        "trigger": "catch me up on",
        "icon": "📋",
        "inputs": ["meeting_description"],
    }


# ── zip folders ────────────────────────────────────────────────────────────


def test_a_zip_folder_is_read_with_its_reference_files():
    data = _zip(
        {
            "launch-review/SKILL.md": SKILL_MD,
            "launch-review/reference/checklist.md": "- Pricing\n- Support\n",
            "launch-review/reference/owners.csv": "area,owner\npricing,pat\n",
        }
    )
    parsed = sf.parse_skill_file("launch-review.zip", data)
    assert [f.path for f in parsed.skill.files] == [
        "reference/checklist.md",
        "reference/owners.csv",
    ]
    assert parsed.skipped == []


def test_a_zip_with_skill_md_at_its_root_is_read():
    parsed = sf.parse_skill_file("x.zip", _zip({"SKILL.md": SKILL_MD, "notes.txt": "n"}))
    assert [f.path for f in parsed.skill.files] == ["notes.txt"]


@pytest.mark.parametrize(
    "path,content,mode,reason",
    [
        ("scripts/check.py", "print('x')", 0o644, "only text and Markdown"),
        ("scripts/run.sh", "echo x", 0o755, "only text and Markdown"),
        ("tool.exe", "MZ", 0o644, "only text and Markdown"),
        ("notes.txt", "plain", 0o755, "marked executable"),
        ("runme.txt", "#!/bin/sh\necho x\n", 0o644, "interpreter line"),
        ("image.md", b"\x89PNG\x00\x00", 0o644, "not text"),
    ],
)
def test_scripts_and_executable_files_are_left_out_and_reported(path, content, mode, reason):
    data = _zip({"s/SKILL.md": SKILL_MD, f"s/{path}": content}, {f"s/{path}": mode})
    parsed = sf.parse_skill_file("s.zip", data)
    assert parsed.skill.files == []
    assert [s.path for s in parsed.skipped] == [path]
    assert reason in parsed.skipped[0].reason


def test_hidden_and_system_files_are_left_out():
    data = _zip({"s/SKILL.md": SKILL_MD, "s/.env": "A=1", "__MACOSX/s/._SKILL.md": "x"})
    parsed = sf.parse_skill_file("s.zip", data)
    assert {s.path for s in parsed.skipped} == {".env", "__MACOSX/s/._SKILL.md"}


def test_an_oversize_reference_file_refuses_the_import():
    data = _zip({"s/SKILL.md": SKILL_MD, "s/big.md": "x" * (sf.MAX_FILE_BYTES + 1)})
    with pytest.raises(sf.SkillError, match="at most"):
        sf.parse_skill_file("s.zip", data)


def test_an_oversize_zip_is_refused_before_it_is_read():
    with pytest.raises(sf.SkillError, match="at most"):
        sf.parse_zip_skill(b"PK\x03\x04" + b"\x00" * sf.MAX_ZIP_BYTES)


def test_files_together_larger_than_a_skill_refuse_the_import():
    entries = {"s/SKILL.md": SKILL_MD}
    for number in range(3):
        entries[f"s/part{number}.md"] = "x" * (sf.MAX_FILE_BYTES - 10)
    with pytest.raises(sf.SkillError, match="together"):
        sf.parse_skill_file("s.zip", _zip(entries))


def test_a_zip_without_skill_md_or_with_two_skills_is_refused():
    with pytest.raises(sf.SkillError, match="no SKILL.md"):
        sf.parse_skill_file("s.zip", _zip({"s/README.md": "x"}))
    with pytest.raises(sf.SkillError, match="more than one"):
        sf.parse_skill_file("s.zip", _zip({"a/SKILL.md": SKILL_MD, "b/SKILL.md": SKILL_MD}))


def test_a_file_that_is_neither_markdown_nor_zip_is_refused():
    with pytest.raises(sf.SkillError, match="SKILL.md"):
        sf.parse_skill_file("skill.py", b"print(1)")


def test_a_zip_whose_entries_cannot_be_read_is_refused_with_a_reason():
    data = bytearray(_zip({"s/SKILL.md": SKILL_MD, "s/notes.md": "some notes here"}))
    # Corrupt the stored bytes of the second entry, so its CRC no longer matches.
    at = bytes(data).index(b"some notes here")
    data[at : at + 4] = b"XXXX"
    with pytest.raises(sf.SkillError, match="could not be read"):
        sf.parse_skill_file("s.zip", bytes(data))


def test_a_corrupt_zip_is_refused():
    with pytest.raises(sf.SkillError, match="not a zip"):
        sf.parse_skill_file("s.zip", b"not a zip")


# ── checking a skill ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "path",
    ["../up.md", "/abs.md", "a/../../b.md", ".hidden.md", "a b.md", "SKILL.md", "a/b/c/d/e.md"],
)
def test_a_reference_file_path_outside_the_folders_rules_is_refused(path):
    with pytest.raises(sf.SkillError):
        sf.check_skill("a", "d", "i", files=[{"Path": path, "Content": "x"}])


def test_a_create_naming_a_script_is_refused():
    with pytest.raises(sf.SkillError, match="only text and Markdown"):
        sf.check_skill("a", "d", "i", files=[{"Path": "run.py", "Content": "x"}])


@pytest.mark.parametrize("name", ["", "Upper", "two--hyphens", "-lead", "x" * 65, "sp ace"])
def test_a_name_outside_the_agent_skills_rules_is_refused(name):
    with pytest.raises(sf.SkillError):
        sf.check_skill(name, "d", "i")


def test_instructions_and_description_are_required_and_capped():
    with pytest.raises(sf.SkillError, match="instructions"):
        sf.check_skill("a", "d", "  ")
    with pytest.raises(sf.SkillError, match="description"):
        sf.check_skill("a", "", "i")
    with pytest.raises(sf.SkillError, match="description"):
        sf.check_skill("a", "x" * (sf.MAX_DESCRIPTION + 1), "i")
    with pytest.raises(sf.SkillError, match="instructions are"):
        sf.check_skill("a", "d", "x" * (sf.MAX_INSTRUCTIONS_BYTES + 1))


def test_too_many_reference_files_are_refused():
    files = [{"Path": f"f{n}.md", "Content": "x"} for n in range(sf.MAX_FILES + 1)]
    with pytest.raises(sf.SkillError, match="at most"):
        sf.check_skill("a", "d", "i", files=files)


def test_unknown_metadata_fields_are_dropped_and_known_ones_checked():
    assert sf.check_metadata({"trigger": "t", "other": "x"}) == {"trigger": "t"}
    assert sf.check_metadata(json.dumps({"inputs": "one"})) == {"inputs": ["one"]}
    with pytest.raises(sf.SkillError):
        sf.check_metadata({"inputs": ["x"] * 21})


# ── writing ────────────────────────────────────────────────────────────────

RECORD = {
    "SkillId": "launch-review",
    "Name": "launch-review",
    "DisplayName": "Launch review",
    "Description": "Review a launch plan.",
    "Instructions": "# Launch review\n\nRead the checklist.\n",
    "Files": [{"Path": "reference/checklist.md", "Content": "- Pricing\n"}],
    "Metadata": {"trigger": "review the launch", "inputs": ["plan"]},
}


def test_a_skill_written_as_skill_md_reads_back_the_same():
    text = sf.render_skill_md(RECORD)
    assert text.startswith("---\nname: launch-review\n")
    skill = sf.parse_skill_file("SKILL.md", text.encode()).skill
    assert (skill.name, skill.description, skill.instructions) == (
        "launch-review",
        "Review a launch plan.",
        RECORD["Instructions"],
    )
    assert skill.display_name == "Launch review"
    assert skill.metadata == RECORD["Metadata"]


def test_a_skill_written_as_a_zip_reads_back_with_its_files():
    data = sf.render_skill_zip(RECORD)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        assert sorted(archive.namelist()) == [
            "launch-review/SKILL.md",
            "launch-review/reference/checklist.md",
        ]
    skill = sf.parse_skill_file("launch-review.zip", data).skill
    assert [(f.path, f.content) for f in skill.files] == [("reference/checklist.md", "- Pricing\n")]


# ── the library ────────────────────────────────────────────────────────────

NOW = datetime(2026, 11, 19, 12, tzinfo=timezone.utc)


def test_the_library_export_is_the_documented_document():
    document = sf.export_library([RECORD], now=NOW)
    assert document["kind"] == "lma-skill-library"
    assert document["version"] == 1
    assert document["exportedAt"] == "2026-11-19T12:00:00Z"
    assert document["skills"][0] == {
        "skillId": "launch-review",
        "name": "launch-review",
        "displayName": "Launch review",
        "description": "Review a launch plan.",
        "instructions": RECORD["Instructions"],
        "files": [{"path": "reference/checklist.md", "content": "- Pricing\n"}],
        "metadata": RECORD["Metadata"],
    }


def test_a_library_round_trip_updates_every_skill_by_name_and_keeps_its_id():
    stored = [{**RECORD, "SkillId": "kept-id"}]
    steps = sf.plan_import(json.dumps(sf.export_library(stored, now=NOW)), stored)
    assert [(s.action, s.skill_id) for s in steps] == [("update", "kept-id")]
    assert steps[0].skill.to_item_fields()["Files"] == RECORD["Files"]


def test_an_import_into_an_empty_deployment_creates_under_the_exported_ids():
    steps = sf.plan_import(sf.export_library([RECORD], now=NOW), [])
    assert [(s.action, s.skill_id) for s in steps] == [("create", "launch-review")]


def test_an_import_gives_a_new_id_when_the_exported_one_is_taken():
    other = {"SkillId": "launch-review", "Name": "something-else"}
    steps = sf.plan_import(sf.export_library([RECORD], now=NOW), [other])
    assert [(s.action, s.skill_id) for s in steps] == [("create", "launch-review-2")]


def test_an_entry_that_cannot_be_read_is_skipped_and_the_rest_imported():
    document = sf.export_library([RECORD], now=NOW)
    document["skills"].append({"name": "Bad Name", "description": "d", "instructions": "i"})
    document["skills"].append(dict(document["skills"][0]))
    steps = sf.plan_import(document, [])
    assert [s.action for s in steps] == ["create", "skip", "skip"]
    assert steps[2].message == "listed more than once"


def test_a_library_entry_with_a_script_keeps_the_skill_and_leaves_the_script_out():
    document = sf.export_library([RECORD], now=NOW)
    document["skills"][0]["files"].append({"path": "run.sh", "content": "echo x"})
    steps = sf.plan_import(document, [])
    assert steps[0].action == "create"
    assert [s.path for s in steps[0].skipped] == ["run.sh"]
    assert [f.path for f in steps[0].skill.files] == ["reference/checklist.md"]


def test_an_import_stops_creating_at_the_skill_limit():
    stored = [{"SkillId": f"s{n}", "Name": f"s{n}"} for n in range(sf.MAX_SKILLS)]
    steps = sf.plan_import(sf.export_library([RECORD], now=NOW), stored)
    assert steps[0].action == "skip" and "at most" in steps[0].message


@pytest.mark.parametrize(
    "document,match",
    [
        ("not json", "not valid JSON"),
        ({"kind": "lma-mcp-server-library", "version": 1}, "not a skill library"),
        ({"kind": "lma-skill-library", "version": 2, "skills": []}, "format version 2"),
        ({"kind": "lma-skill-library", "version": 1}, "no list of skills"),
    ],
)
def test_a_document_that_is_not_a_skill_library_is_refused(document, match):
    with pytest.raises(sf.SkillError, match=match):
        sf.plan_import(document, [])


def test_the_caps_fit_the_library_export_in_one_appsync_response():
    # Every skill at its largest, with a fifth again for JSON escaping and the
    # document's own fields.
    assert sf.MAX_SKILLS * sf.MAX_SKILL_BYTES * 1.2 < 5 * 1024 * 1024
