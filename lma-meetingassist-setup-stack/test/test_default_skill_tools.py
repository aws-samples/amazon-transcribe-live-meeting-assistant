#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""The default skills LMA ships name only the meeting assistant's own tools.

A skill cannot give the assistant a tool, so a default skill that names one the
assistant does not have would send it looking for something it cannot call.
Every identifier a default skill's instructions quote in backticks that looks
like a tool or argument name (``snake_case``) has to be a tool the meeting
assistant defines with ``@tool``, or an argument of one. The registry is read
from the assistant's source rather than a list kept here, so a renamed tool
fails this test.

The skills are the configuration seeder's ``default_skills/*/SKILL.md``.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
ASSISTANT_SOURCES = sorted((REPO / "lma-meetingassist-setup-stack" / "src").glob("*.py"))
DEFAULT_SKILLS = sorted(
    (REPO / "lma-ai-stack/source/lambda_functions/configuration_seeder/default_skills").glob(
        "*/SKILL.md"
    )
)
#: The meeting assistant's own sources, not the troubleshooting agent's tools.
NOT_THE_MEETING_ASSISTANT = ("troubleshooting_",)
QUOTED_IDENTIFIER = re.compile(r"`([a-z][a-z0-9]*(?:_[a-z0-9]+)+)`")


def _is_tool(decorator: ast.expr) -> bool:
    target = decorator.func if isinstance(decorator, ast.Call) else decorator
    return isinstance(target, ast.Name) and target.id == "tool"


def assistant_tools() -> dict:
    """Tool name -> its argument names, for every ``@tool`` function the assistant defines."""
    tools = {}
    for path in ASSISTANT_SOURCES:
        if path.name.startswith(NOT_THE_MEETING_ASSISTANT):
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
                _is_tool(d) for d in node.decorator_list
            ):
                tools[node.name] = {a.arg for a in node.args.args + node.args.kwonlyargs}
    return tools


def test_the_registry_holds_the_assistants_tools():
    tools = assistant_tools()
    for name in ("current_meeting_transcript", "meeting_history", "load_skill", "web_search"):
        assert name in tools
    assert "with_timestamps" in tools["current_meeting_transcript"]


def test_the_default_skills_are_found():
    assert len(DEFAULT_SKILLS) == 5


@pytest.mark.parametrize("path", DEFAULT_SKILLS, ids=lambda p: p.parent.name)
def test_a_default_skill_names_only_the_assistants_tools_and_their_arguments(path):
    tools = assistant_tools()
    arguments = set().union(*tools.values())
    quoted = set(QUOTED_IDENTIFIER.findall(path.read_text(encoding="utf-8")))
    unknown = sorted(name for name in quoted if name not in tools and name not in arguments)
    assert unknown == [], f"{path.parent.name} names what the assistant does not have: {unknown}"
    assert quoted & set(tools), f"{path.parent.name} names no tool of the assistant"
