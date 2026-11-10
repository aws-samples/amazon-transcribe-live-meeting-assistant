# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""The copies of key_in_folder are one rule.

Each function that accepts an S3 key only inside a meeting's folder carries its
own copy of ``key_in_folder``: the functions share no module that every one of
them loads. This test keeps the copies' text identical, and runs each copy
against the same table of keys, so a change to one copy fails here until every
copy has it.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, Callable, Dict

import pytest

FUNCTIONS = Path(__file__).resolve().parents[1] / "source" / "lambda_functions"

COPIES = [
    FUNCTIONS / "video_keyframes_resolver" / "index.py",
    FUNCTIONS / "keyframe_text_extractor" / "pure.py",
    FUNCTIONS / "meeting_consent_resolver" / "index.py",
    FUNCTIONS / "consent_evidence_verifier" / "index.py",
]

# A meeting folder whose name, like a posixified CallId, keeps a run of dots.
FOLDER = "lma-video-keyframes/UI_changes.._prep_to_release_2026_11_09/"

KEYS = [
    (f"{FOLDER}0000001000-ab12cd.jpg", True),
    (f"{FOLDER}sub/frame.jpg", True),
    (f"{FOLDER}frame..jpg", True),
    (f"{FOLDER}...", True),
    (f"{FOLDER}../Another_meeting/f.jpg", False),
    (f"{FOLDER}a/../../x.wav", False),
    (f"{FOLDER}a/..", False),
    (f"{FOLDER}./f.jpg", False),
    (f"{FOLDER}a//f.jpg", False),
    (f"{FOLDER}a/", False),
    (FOLDER, False),
    ("lma-video-keyframes/Another_meeting/f.jpg", False),
    ("lma-video-keyframes/UI_changes.._prep_to_release_2026_11_09.jpg", False),
    (None, False),
    (42, False),
]


def _source(path: Path) -> str:
    text = path.read_text()
    for node in ast.parse(text).body:
        if isinstance(node, ast.FunctionDef) and node.name == "key_in_folder":
            return ast.get_source_segment(text, node) or ""
    raise AssertionError(f"{path} defines no key_in_folder")


def _compiled(path: Path) -> Callable[[Any, str], bool]:
    namespace: Dict[str, Any] = {"Any": Any}
    exec(compile(_source(path), str(path), "exec"), namespace)  # noqa: S102  # nosec B102
    return namespace["key_in_folder"]


def test_every_copy_of_key_in_folder_has_the_same_text() -> None:
    sources = {path.relative_to(FUNCTIONS).as_posix(): _source(path) for path in COPIES}
    assert len(set(sources.values())) == 1, sorted(sources)


@pytest.mark.parametrize("path", COPIES, ids=lambda p: p.parent.name)
@pytest.mark.parametrize("key, inside", KEYS)
def test_every_copy_accepts_exactly_the_keys_inside_the_folder(path, key, inside) -> None:
    assert _compiled(path)(key, FOLDER) is inside


@pytest.mark.parametrize("path", COPIES, ids=lambda p: p.parent.name)
def test_every_copy_refuses_a_folder_without_its_trailing_slash(path) -> None:
    assert _compiled(path)(f"{FOLDER}f.jpg", FOLDER.rstrip("/")) is False


def test_no_function_checks_a_key_for_dots_as_a_substring() -> None:
    for path in COPIES:
        assert '".." in key' not in path.read_text(), path
