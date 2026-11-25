# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""The role matrix (lma-ai-stack/source/appsync/role-matrix.json) and the doc table made from it.

test_role_matrix.py holds the schema, the Cognito template and the role table in
docs/user-based-access-control.md to the matrix. After changing the matrix,
regenerate the table with:

    python3 lma-ai-stack/test/role_matrix.py --write-docs
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MATRIX_PATH = REPO_ROOT / "lma-ai-stack" / "source" / "appsync" / "role-matrix.json"
DOC_PATH = REPO_ROOT / "docs" / "user-based-access-control.md"
TABLE_START = "<!-- role-table:start -->"
TABLE_END = "<!-- role-table:end -->"


def load() -> dict:
    return json.loads(MATRIX_PATH.read_text(encoding="utf-8"))


def appsync_roles(matrix: dict) -> dict[str, tuple[str, ...]]:
    """Every AppSync operation ("Type.field") and the signed-in roles it admits."""
    roles: dict[str, tuple[str, ...]] = {}
    for capability in matrix["capabilities"]:
        for operation in capability["appsync"]:
            roles[operation] = tuple(capability["roles"])
    for operation in matrix["backendOnly"]["appsync"]:
        roles[operation] = ()
    return roles


def render_table(matrix: dict) -> str:
    """The role table of the doc page, one row per capability."""
    names = list(matrix["roles"])
    lines = [
        TABLE_START,
        "| Capability | " + " | ".join(names) + " | Notes |",
        "|---|" + "---|" * len(names) + "---|",
    ]
    for capability in matrix["capabilities"]:
        marks = ["Yes" if name in capability["roles"] else "No" for name in names]
        lines.append(
            f"| {capability['title']} | " + " | ".join(marks) + f" | {capability['notes']} |"
        )
    lines.append(TABLE_END)
    return "\n".join(lines)


def doc_table(text: str) -> str | None:
    """The generated table as the doc page holds it, markers included."""
    start = text.find(TABLE_START)
    end = text.find(TABLE_END)
    if start < 0 or end < start:
        return None
    return text[start : end + len(TABLE_END)]


def write_docs() -> None:
    text = DOC_PATH.read_text(encoding="utf-8")
    current = doc_table(text)
    if current is None:
        raise SystemExit(f"{DOC_PATH} has no {TABLE_START} ... {TABLE_END} block")
    DOC_PATH.write_text(text.replace(current, render_table(load())), encoding="utf-8")


if __name__ == "__main__":
    if sys.argv[1:] == ["--write-docs"]:
        write_docs()
    else:
        print(render_table(load()))
