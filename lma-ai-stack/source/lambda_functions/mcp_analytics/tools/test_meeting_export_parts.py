# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""The parts of a meeting export are named alike in all three places that list them.

The meeting archive function builds the export from its PARTS; the MCP tool
checks ``include`` against its own copy before it invokes that function; and the
tool's definition offers the same names in its ``include`` enum. A part added to
one and not the others is refused by the tool, or never offered.
"""

import ast
from pathlib import Path

from tool_definitions import MEETING_TOOLS

from tools import meeting_export

ARCHIVE_EXPORT = Path(__file__).resolve().parents[2] / "meeting_archive" / "export.py"


def _archive_parts():
    """export.PARTS, read from the source so the archive's layer imports are not needed."""
    for node in ast.parse(ARCHIVE_EXPORT.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "PARTS" for t in node.targets
        ):
            return tuple(ast.literal_eval(node.value))
    raise AssertionError("meeting_archive/export.py has no PARTS")


def test_the_tool_takes_every_part_the_archive_builds():
    assert meeting_export.PARTS == _archive_parts()
    assert "whiteboards" in meeting_export.PARTS


def test_the_tool_offers_every_part_it_takes():
    tool = next(t for t in MEETING_TOOLS if t["name"] == "get_meeting_export")
    offered = tool["inputSchema"]["properties"]["include"]["items"]["enum"]
    assert tuple(offered) == meeting_export.PARTS


def test_include_whiteboards_is_accepted():
    assert meeting_export.include_argument(["whiteboards"]) == ["whiteboards"]
