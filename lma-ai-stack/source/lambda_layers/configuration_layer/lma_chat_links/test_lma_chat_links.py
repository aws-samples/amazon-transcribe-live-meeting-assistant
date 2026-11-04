# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""The web addresses found in meeting chat, and their removal from a model's text."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import lma_chat_links as links  # noqa: E402

FIXTURE = HERE.parents[4] / "test-fixtures" / "chat-links.json"


def _expand(text: str) -> str:
    return re.sub(r"LONG(\d+)", lambda m: "a" * int(m.group(1)), text)


CASES = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_the_addresses_found_match_the_shared_table(case) -> None:
    assert links.extract_links(_expand(case["input"])) == [_expand(u) for u in case["urls"]]


def test_the_length_cases_sit_either_side_of_the_limit() -> None:
    assert len(_expand("https://example.com/LONG2028")) == links.MAX_LINK_CHARS
    assert len(_expand("https://example.com/LONG2040")) > links.MAX_LINK_CHARS


def test_anything_but_text_has_no_addresses() -> None:
    assert links.extract_links(None) == []
    assert links.extract_links(42) == []


def test_addresses_across_several_messages_are_listed_once() -> None:
    assert links.extract_links_from(
        ["https://a.example/1", None, "www.b.example and https://a.example/1"]
    ) == ["https://a.example/1", "https://www.b.example"]


@pytest.mark.parametrize(
    "text, expected",
    [
        ("see [the deck](https://evil.example/x) now", "see the deck now"),
        ("![pixel](https://evil.example/p.png)", "pixel"),
        ("[a](https://x.example/a_(b)) done", "a done"),
        ("go to https://evil.example/x.", "go to (link)."),
        ("(https://evil.example/x)", "((link))"),
        ("www.evil.example/a, then", "(link), then"),
        ("docs.example.com/path here", "(link) here"),
        ("<https://evil.example/auto>", ""),
        ('<a href="https://evil.example">click</a>', "click"),
        ("[click][1]\n[1]: https://evil.example", "click\n"),
        ("ftp://files.example/f and javascript:alert(1)", "(link) and (link)"),
        ("- **Budget**: Alice asked about Node.js and e.g. costs", None),
    ],
)
def test_links_and_pictures_are_taken_out_of_a_models_text(text, expected) -> None:
    assert links.strip_links(text) == (text if expected is None else expected)


def test_stripping_anything_but_text_gives_empty_text() -> None:
    assert links.strip_links(None) == ""
