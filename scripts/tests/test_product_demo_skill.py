# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""The product-demo skill and its storyboard file.

The skill is a procedure an agent follows against a live stack, so nothing else
exercises it. These pin the parts a run would otherwise discover the hard way:
that the recorder is started in demo mode, that the user is asked before anything
is recorded, that the storyboard file parses into what the skill reads, and that
every file and make target the skill points at exists.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL = REPO_ROOT / ".claude" / "skills" / "product-demo.md"
STORYBOARDS = REPO_ROOT / "scripts" / "demo_storyboards.yaml"


def _session_module():
    spec = importlib.util.spec_from_file_location(
        "ux_test_session", REPO_ROOT / "scripts" / "ux_test_session.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("ux_test_session", module)
    spec.loader.exec_module(module)
    return module


#: Every role the session helper can create: the template's groups, plus User.
VALID_PERSONAS = set(_session_module().role_names())
REQUIRED_FIELDS = {
    "id",
    "title",
    "source",
    "audience",
    "hook",
    "persona",
    "duration_target",
    "fixtures",
    "chapters",
    "closing",
    "takeaways",
    "not_shown",
    "docs",
    "category",
    "last_recorded",
}


def _skill() -> str:
    return SKILL.read_text(encoding="utf-8")


def _storyboards() -> list[dict]:
    with STORYBOARDS.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)["storyboards"]


@pytest.mark.unit
class TestStoryboardsFile:
    def test_parses_and_every_entry_has_what_the_skill_reads(self):
        boards = _storyboards()
        assert boards, "the file should hold at least the format example"
        for board in boards:
            missing = REQUIRED_FIELDS - set(board)
            assert not missing, f"storyboard {board.get('id')!r} lacks {sorted(missing)}"

    def test_ids_are_unique_slugs(self):
        ids = [b["id"] for b in _storyboards()]
        assert len(ids) == len(set(ids))
        for slug in ids:
            assert slug == slug.lower() and " " not in slug, slug

    def test_personas_are_roles_the_session_helper_can_create(self):
        for board in _storyboards():
            assert board["persona"] in VALID_PERSONAS, board["id"]

    def test_chapters_carry_narration_and_steps(self):
        for board in _storyboards():
            assert 3 <= len(board["chapters"]) <= 10, (
                f"{board['id']}: a demo is five to eight chapters, not a checklist"
            )
            for chapter in board["chapters"]:
                assert chapter.get("label") and chapter.get("say") and chapter.get("do"), (
                    f"{board['id']}: chapter {chapter.get('label')!r} is incomplete"
                )

    def test_takeaways_fit_the_end_card(self):
        """render puts the first five takeaways on the end card; more are lost."""
        for board in _storyboards():
            assert 1 <= len(board["takeaways"]) <= 5, board["id"]

    def test_narration_never_names_ids_or_clicks(self):
        for board in _storyboards():
            spoken = " ".join(
                [board["hook"], board["closing"]] + [c["say"] for c in board["chapters"]]
            ).lower()
            for banned in ("click", "uid ", "stack", "@"):
                assert banned not in spoken, f"{board['id']}: narration says {banned!r}"

    def test_docs_targets_exist(self):
        for board in _storyboards():
            page = board["docs"].split("#")[0]
            assert (REPO_ROOT / page).exists(), f"{board['id']}: {page} is missing"

    def test_fixture_files_named_in_the_storyboards_exist(self):
        for board in _storyboards():
            for path in re.findall(r"[\w./-]+\.wav", " ".join(board["fixtures"])):
                assert (REPO_ROOT / path).exists(), f"{board['id']}: {path} is missing"

    def test_categories_are_headings_of_the_demo_videos_page(self):
        page = (REPO_ROOT / "docs" / "demo-videos.md").read_text(encoding="utf-8")
        for board in _storyboards():
            assert f"## {board['category']}" in page, (
                f"{board['id']}: {board['category']!r} is not a docs/demo-videos.md heading"
            )


@pytest.mark.unit
class TestTheSkill:
    def test_starts_the_recorder_in_demo_mode(self):
        text = _skill()
        for needle in (
            "ux_recorder.py start --kind demo",
            "--title",
            "--subtitle",
            "ux_recorder.py mark",
            "ux_recorder.py stop",
            "ux_recorder.py render",
            "demo.md",
            "demo.mp4",
            "scratch/ux-recordings",
        ):
            assert needle in text, needle

    def test_proposes_three_and_asks_before_recording(self):
        text = _skill()
        assert "three" in text.lower()
        assert "AskUserQuestion" in text
        assert "rehears" in text.lower(), "a demo must be rehearsed before recording"

    def test_every_repository_path_it_names_exists(self):
        """The skill was ported from another repository; a path left over from it
        would send a run looking for a file that is not here."""
        text = _skill()
        paths = set(
            re.findall(
                r"`((?:\.claude|scripts|docs|integ-tests|utilities|lma-[\w-]+)/[\w./-]+)`", text
            )
        )
        assert ".claude/skills/pr-review.md" in paths
        assert "scripts/demo_storyboards.yaml" in paths
        for path in sorted(paths):
            if "…" in path or "<" in path:
                continue
            assert (REPO_ROOT / path.rstrip("/")).exists(), path

    def test_every_make_target_it_names_exists(self):
        makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
        for target in set(re.findall(r"`make ([\w-]+)", _skill())):
            assert re.search(rf"^{target}:", makefile, re.M), target

    def test_states_the_privacy_rule_and_the_no_commit_rule(self):
        lowered = _skill().lower()
        assert "never commit" in lowered
        assert "ask once" in lowered or "say it once" in lowered
        assert "not deployed" in lowered, "blocked-not-deployed must be a named outcome"
        prose = " ".join(lowered.split())
        assert "events.jsonl" in prose and "never commit the session directory" in prose, (
            "the rule must cover the click log and the whole session, not the video"
        )
        assert "deployment info" in prose, "the stack name in the navigation must be named"

    def test_insists_on_teardown_of_a_created_user(self):
        prose = " ".join(_skill().lower().split())
        assert "always run the teardown it prints" in prose

    def test_hands_over_a_docs_entry_draft(self):
        text = _skill()
        assert "docs-entry.md" in text and "docs/demo-videos.md" in text
        assert "user-attachments" in text

    def test_is_registered_in_claude_md(self):
        claude_md = (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
        assert ".claude/skills/product-demo.md" in claude_md

    def test_the_demo_videos_page_is_in_the_docs_index(self):
        index = (REPO_ROOT / "docs" / "INDEX.md").read_text(encoding="utf-8")
        assert "(demo-videos.md)" in index
