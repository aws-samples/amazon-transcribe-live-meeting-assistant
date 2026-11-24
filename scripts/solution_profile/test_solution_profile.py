# Copyright (c) 2025 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Tests for the solution profile, its check and its release build.

Run with ``make test-solution-profile``. Needs PyYAML, jsonschema and git; no AWS.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sys
import tarfile
from pathlib import Path

import pytest
import yaml

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import build  # noqa: E402
import check  # noqa: E402

REPO = HERE.parents[1]
PROFILE = REPO / "solution-profile"
COMMIT = "0123456789abcdef0123456789abcdef01234567"


# ---------------------------------------------------------------------------
# The vendored contract
# ---------------------------------------------------------------------------


def test_vendored_contract_files_match_their_pinned_digests():
    readme = (PROFILE / "schema" / "README.md").read_text(encoding="utf-8")
    block = readme.split("<!-- vendored-sha256:start -->", 1)[1].split(
        "<!-- vendored-sha256:end -->", 1
    )[0]
    pins = dict(
        (name, digest)
        for digest, name in re.findall(r"^([0-9a-f]{64})\s+(\S+)$", block, flags=re.MULTILINE)
    )
    assert set(pins) == {
        "v1/README.md",
        "v1/solution-profile.schema.json",
        "v1/validate_profile.py",
    }
    for name, digest in pins.items():
        actual = hashlib.sha256((PROFILE / "schema" / name).read_bytes()).hexdigest()
        assert actual == digest, f"solution-profile/schema/{name} differs from its pinned digest"


def _pins(text, pattern):
    found = re.findall(pattern, text, flags=re.MULTILINE)
    return {name.lower(): version for name, version in found}


def test_the_check_and_the_release_build_install_the_validators_versions():
    """make setup-python and the release workflow install what the validator declares.

    So a passing ``make solution-profile-check`` predicts the release build.
    """
    validator = (PROFILE / "schema" / "v1" / "validate_profile.py").read_text(encoding="utf-8")
    declared = _pins(
        re.search(r"^# dependencies = \[(.*)\]$", validator, flags=re.MULTILINE).group(1),
        r'"(pyyaml|jsonschema)==([0-9.]+)"',
    )
    assert set(declared) == {"pyyaml", "jsonschema"}, declared
    workflow = (REPO / ".github" / "workflows" / "solution-profile-release.yml").read_text(
        encoding="utf-8"
    )
    assert _pins(workflow, r'"(pyyaml|jsonschema)==([0-9.]+)"') == declared
    makefile = (REPO / "Makefile").read_text(encoding="utf-8")
    setup = makefile.split("\nsetup-python:", 1)[1].split("\n\n", 1)[0]
    assert _pins(setup, r"^\t\t(pyyaml|jsonschema)==([0-9.]+) \\$") == declared


def test_contract_directory_holds_only_the_vendored_files():
    schema = PROFILE / "schema"
    files = sorted(
        p.relative_to(schema).as_posix()
        for p in schema.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts
    )
    assert files == [
        "README.md",
        "v1/README.md",
        "v1/solution-profile.schema.json",
        "v1/validate_profile.py",
    ]


# ---------------------------------------------------------------------------
# The profile as it stands
# ---------------------------------------------------------------------------


def test_the_profile_passes_the_validator_and_every_cross_check(capsys):
    status = check.main(["--repo-root", str(REPO)])
    out = capsys.readouterr().out
    assert status == 0, out
    assert "cross-checks: passed" in out


def test_the_manifest_has_the_required_identity_and_files():
    manifest = yaml.safe_load((PROFILE / "solution-profile.yaml").read_text(encoding="utf-8"))
    assert manifest["id"] == "live-meeting-assistant"
    assert manifest["support"]["model"] == "community-sample"
    assert "tag" not in manifest["source"] and "commit" not in manifest["source"]
    for key in ("sizing", "delivery"):
        assert (PROFILE / manifest["files"][key]).is_file()
    assert manifest["skills"] == ["skills/lma-solution-fit"]
    skill = (PROFILE / "skills" / "lma-solution-fit" / "SKILL.md").read_text(encoding="utf-8")
    front = yaml.safe_load(skill.split("---", 2)[1])
    assert front["name"] == "lma-solution-fit" and front["description"]


def test_the_manifest_version_is_the_contract_form_of_VERSION():
    manifest = yaml.safe_load((PROFILE / "solution-profile.yaml").read_text(encoding="utf-8"))
    pep440 = (REPO / "VERSION").read_text(encoding="utf-8")
    assert manifest["version"] == check.contract_version(pep440)


def test_universes_read_from_the_code_are_not_empty():
    tools = check.mcp_tools(REPO)
    assert {"search_lma_meetings", "get_meeting_export", "list_virtual_participants"} <= tools
    assert check.vp_platforms(REPO) == {"ZOOM", "TEAMS", "CHIME", "WEBEX", "GOOGLE_MEET"}
    assert "standard" in check.preset_names(REPO)
    assert "us-east-1" in check.release_regions(REPO)


def test_mcp_tool_universe_matches_the_server_definitions_by_ast():
    # The original tools are a plain literal, so literal_eval and the name walk agree.
    import ast

    literal = ast.literal_eval(check._module_assignment(REPO / check.MCP_INDEX, "MCP_TOOLS"))
    assert {t["name"] for t in literal} == check._tool_names(REPO / check.MCP_INDEX, "MCP_TOOLS")


# ---------------------------------------------------------------------------
# The check fails, with a clear message, when the code moves ahead of the profile
# ---------------------------------------------------------------------------


@pytest.fixture
def repo_copy(tmp_path: Path) -> Path:
    """A copy of the files the check reads: the profile, its evidence and the sources."""
    root = tmp_path / "repo"
    shutil.copytree(PROFILE, root / "solution-profile")
    for name in ("VERSION", "CHANGELOG.md", "aws-release.sh"):
        shutil.copy2(REPO / name, root / name)
    for entry in check.evidence_entries(PROFILE):
        rel = entry[2].split("#", 1)[0]
        if rel.startswith("https://"):
            continue
        src, dst = REPO / rel, root / rel
        if dst.exists():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            dst.mkdir(exist_ok=True)  # existence is all the validator asks of a directory
        else:
            shutil.copy2(src, dst)
    for rel in (check.MCP_INDEX, check.MCP_MEETING_TOOLS, check.PLATFORM_HELPER):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO / rel, root / rel)
    shutil.rmtree(root / check.PRESETS_DIR, ignore_errors=True)
    shutil.copytree(REPO / check.PRESETS_DIR, root / check.PRESETS_DIR)
    return root


def _run(root: Path, capsys) -> tuple[int, str]:
    status = check.main(["--repo-root", str(root)])
    return status, capsys.readouterr().out


def test_repo_copy_passes_as_it_stands(repo_copy, capsys):
    status, out = _run(repo_copy, capsys)
    assert status == 0, out


def test_a_new_mcp_tool_without_a_profile_entry_fails(repo_copy, capsys):
    path = repo_copy / check.MCP_MEETING_TOOLS
    text = path.read_text(encoding="utf-8")
    text = text.replace(
        "MEETING_TOOLS: List[Dict[str, Any]] = [\n",
        'MEETING_TOOLS: List[Dict[str, Any]] = [\n    {"name": "summon_unicorn", "annotations": WRITE},\n',
        1,
    )
    path.write_text(text, encoding="utf-8")
    status, out = _run(repo_copy, capsys)
    assert status == 1
    assert "no capability is tagged 'mcp-tool:summon_unicorn'" in out


def test_a_removed_mcp_tool_still_tagged_fails(repo_copy, capsys):
    path = repo_copy / check.MCP_INDEX
    text = path.read_text(encoding="utf-8")
    path.write_text(
        text.replace('"name": "get_virtual_participant_status"', '"name": "renamed_status"'),
        encoding="utf-8",
    )
    status, out = _run(repo_copy, capsys)
    assert status == 1
    assert "'mcp-tool:get_virtual_participant_status', which" in out
    assert "no capability is tagged 'mcp-tool:renamed_status'" in out


def test_a_new_vp_platform_without_a_profile_entry_fails(repo_copy, capsys):
    path = repo_copy / check.PLATFORM_HELPER
    text = path.read_text(encoding="utf-8")
    path.write_text(
        text.replace(
            '"meet": "GOOGLE_MEET",', '"meet": "GOOGLE_MEET",\n    "gotomeeting": "GOTO",'
        ),
        encoding="utf-8",
    )
    status, out = _run(repo_copy, capsys)
    assert status == 1
    assert "no capability is tagged 'vp-platform:GOTO'" in out


def test_a_new_preset_without_a_profile_entry_fails(repo_copy, capsys):
    presets = repo_copy / check.PRESETS_DIR
    shutil.copy2(presets / "standard.json", presets / "school-district.json")
    status, out = _run(repo_copy, capsys)
    assert status == 1
    assert "no capability is tagged 'preset:school-district'" in out


def test_a_new_release_region_without_a_profile_entry_fails(repo_copy, capsys):
    path = repo_copy / "aws-release.sh"
    path.write_text(
        path.read_text(encoding="utf-8")
        + "\n./publish.sh some-bucket artifacts/lma eu-central-1 public\n",
        encoding="utf-8",
    )
    status, out = _run(repo_copy, capsys)
    assert status == 1
    assert "regions lack ['eu-central-1']" in out


def test_a_new_changelog_release_without_a_changes_entry_fails(repo_copy, capsys):
    path = repo_copy / "CHANGELOG.md"
    text = path.read_text(encoding="utf-8")
    path.write_text(
        text.replace(
            "## [Unreleased]",
            "## [Unreleased]\n\n## [9.9.9] - 2099-01-01\n\n- ⚠️ **Do something.**\n",
            1,
        ),
        encoding="utf-8",
    )
    status, out = _run(repo_copy, capsys)
    assert status == 1
    assert "changes.yaml: no entry for 9.9.9" in out


def test_a_release_with_an_action_item_needs_action_required():
    profile = check.Profile(releases=[{"version": "1.2.3", "summary": "x"}])
    problems = check.check_changes(profile, [("1.2.3", "- ⚠️ **Rotate the key.**")])
    assert problems and "action_required is empty" in problems[0]
    assert check.check_changes(profile, [("1.2.3", "- **A fix.**")]) == []


def test_a_missing_evidence_file_fails(repo_copy, capsys):
    (repo_copy / "docs" / "meeting-export.md").unlink()
    status, out = _run(repo_copy, capsys)
    assert status == 1
    assert "evidence path does not exist: docs/meeting-export.md" in out


def test_a_renamed_heading_breaks_its_evidence_anchor(repo_copy, capsys):
    path = repo_copy / "docs" / "voice-assistant.md"
    text = path.read_text(encoding="utf-8")
    path.write_text(
        text.replace("### Nova Sonic Concurrency Quota", "### Nova Sonic Limits"), encoding="utf-8"
    )
    status, out = _run(repo_copy, capsys)
    assert status == 1
    assert "evidence anchor '#nova-sonic-concurrency-quota' matches no heading" in out


def test_release_rules_need_a_review_and_a_changes_entry(repo_copy, capsys):
    (repo_copy / "VERSION").write_text("9.9.9\n", encoding="utf-8")
    check.stamp_manifest_version(repo_copy / "solution-profile" / "solution-profile.yaml", "9.9.9")
    status, out = _run(repo_copy, capsys)
    assert status == 1
    assert "release rules ON" in out
    assert "reviewed_for_version" in out and "changes: no entry for version 9.9.9" in out
    assert "Review the solution profile" in out


# ---------------------------------------------------------------------------
# Versions and anchors
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pep440", "contract", "final"),
    [
        ("0.4.1", "0.4.1", True),
        ("0.4.1.dev1", "0.4.1-dev1", False),
        ("0.4.1rc2", "0.4.1-rc2", False),
        ("0.4.1.post1\n", "0.4.1-post1", False),
    ],
)
def test_contract_version(pep440, contract, final):
    assert check.contract_version(pep440) == contract
    assert check.is_final_release(pep440) is final


def test_contract_version_refuses_a_malformed_version():
    with pytest.raises(ValueError):
        check.contract_version("v0.4")


def test_stamp_manifest_version_rewrites_only_the_version_line(tmp_path):
    manifest = tmp_path / "solution-profile.yaml"
    manifest.write_text("# c\nversion: 0.1.0\nreviewed_for_version: 0.1.0\n", encoding="utf-8")
    assert check.stamp_manifest_version(manifest, "0.2.0.dev1") is True
    assert (
        manifest.read_text(encoding="utf-8")
        == "# c\nversion: 0.2.0-dev1\nreviewed_for_version: 0.1.0\n"
    )
    assert check.stamp_manifest_version(manifest, "0.2.0.dev1") is False


def test_anchors_follow_github_slugger():
    text = "# Title\n## `lma deploy` — Deploy/Update Stack\n## Limits & Best Practices\n## Limits\n## Limits\n```\n## not a heading\n```\n"
    assert check.anchors(text) == {
        "title",
        "lma-deploy--deployupdate-stack",
        "limits--best-practices",
        "limits",
        "limits-1",
    }


# ---------------------------------------------------------------------------
# The release build
# ---------------------------------------------------------------------------


def _build(out: Path) -> Path:
    return build.build(REPO, out, tag="v9.9.9", commit=COMMIT)


def test_two_builds_produce_identical_bytes(tmp_path):
    first = _build(tmp_path / "a")
    second = _build(tmp_path / "b")
    assert first.name == second.name
    assert first.read_bytes() == second.read_bytes()
    for suffix in (".sha256",):
        assert (first.parent / f"{first.name}{suffix}").read_bytes() == (
            second.parent / f"{second.name}{suffix}"
        ).read_bytes()


def test_the_asset_is_named_and_hashed_as_the_contract_says(tmp_path):
    target = _build(tmp_path)
    manifest = yaml.safe_load((PROFILE / "solution-profile.yaml").read_text(encoding="utf-8"))
    assert target.name == f"solution-profile-live-meeting-assistant-{manifest['version']}.tar.gz"
    digest, name = (tmp_path / f"{target.name}.sha256").read_text(encoding="utf-8").split()
    assert name == target.name
    assert digest == hashlib.sha256(target.read_bytes()).hexdigest()


def test_the_tarball_holds_the_profile_and_build_json_but_not_the_contract(tmp_path):
    target = _build(tmp_path)
    with tarfile.open(target, "r:gz") as tar:
        members = tar.getmembers()
        names = [m.name for m in members]
        assert all(n == "solution-profile" or n.startswith("solution-profile/") for n in names)
        assert not any(n.startswith("solution-profile/schema") for n in names)
        assert "solution-profile/skills/lma-solution-fit/SKILL.md" in names
        assert names == sorted(names)
        assert all(m.mtime == 0 and m.uid == 0 and m.gid == 0 and m.uname == "" for m in members)
        info = json.loads(tar.extractfile("solution-profile/build.json").read())
        manifest = yaml.safe_load(tar.extractfile("solution-profile/solution-profile.yaml").read())
    assert info == {
        "id": "live-meeting-assistant",
        "version": manifest["version"],
        "tag": "v9.9.9",
        "commit": COMMIT,
        "schema_version": 1,
    }
    assert manifest["source"]["tag"] == "v9.9.9" and manifest["source"]["commit"] == COMMIT
    side = json.loads((tmp_path / target.name.replace(".tar.gz", ".build.json")).read_text())
    assert side == info


def test_the_gzip_header_carries_no_time_or_name(tmp_path):
    raw = _build(tmp_path).read_bytes()
    assert raw[:3] == b"\x1f\x8b\x08"
    assert raw[3] == 0  # no FNAME, FCOMMENT or FEXTRA
    assert raw[4:8] == b"\x00\x00\x00\x00"  # mtime 0


def test_the_unpacked_tarball_passes_the_validator(tmp_path, capsys):
    target = _build(tmp_path)
    status = build.validate_unpacked(REPO, target, False, build.expected_version(REPO))
    assert status == 0, capsys.readouterr().out


def test_stamp_source_refuses_a_manifest_that_already_has_a_commit():
    text = "source:\n  repository: https://example.com/r\n  commit: " + COMMIT + "\nsummary: x\n"
    with pytest.raises(ValueError):
        build.stamp_source(text, "v1.0.0", COMMIT)


def test_stamp_source_refuses_a_short_commit():
    with pytest.raises(ValueError):
        build.stamp_source("source:\n  repository: https://example.com/r\n", "v1.0.0", "abc123")


def test_profile_files_skip_the_contract_and_hidden_files(tmp_path):
    root = tmp_path / "solution-profile"
    (root / "schema" / "v1").mkdir(parents=True)
    (root / "schema" / "v1" / "x.json").write_text("{}")
    (root / ".DS_Store").write_text("")
    (root / "capabilities.yaml").write_text("capabilities: []\n")
    assert [p.as_posix() for p in build.profile_files(root)] == ["capabilities.yaml"]
