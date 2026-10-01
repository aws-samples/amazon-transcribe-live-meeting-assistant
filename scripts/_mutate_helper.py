#!/usr/bin/env python3
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Helper for scripts/mutate.sh: parse the mutation file, and apply one mutation.

A separate file rather than a heredoc inside the shell function, because an
indented heredoc terminator does not terminate — the first version of this
silently fed shell lines to python and reported every mutation as NOT-APPLIED.

Two subcommands:

    parse <mutation-file> <work-dir>   write one JSON spec per mutation, print the count
    apply <spec.json> <worktree>       apply that mutation inside the worktree

`apply` exits 0 when the file really changed, 3 when it did not. Exit 3 is the
important one: a pattern that no longer matches is a silent no-op, and a no-op
reads exactly like a caught mutation if only the test's exit code is consulted.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys


def parse(mutation_file: str, work_dir: str) -> int:
    """Split the mutation file into one spec per stanza."""
    source = pathlib.Path(mutation_file)
    stanzas: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for raw in source.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            if current:
                stanzas.append(current)
                current = {}
            continue
        if raw.lstrip().startswith("#"):
            continue
        key, _, value = raw.partition(":")
        current[key.strip()] = value.lstrip()
    if current:
        stanzas.append(current)

    specs = pathlib.Path(work_dir) / "specs"
    specs.mkdir(parents=True, exist_ok=True)
    for index, stanza in enumerate(stanzas):
        missing = [k for k in ("name", "file") if k not in stanza]
        has_edit = ({"old", "new"} <= set(stanza)) or ("sed" in stanza)
        if missing or not has_edit:
            print(
                f"mutation {index} is incomplete (needs name, file, and either "
                f"old+new or sed): {stanza}",
                file=sys.stderr,
            )
            return 2
        (specs / f"{index:03d}.json").write_text(json.dumps(stanza), encoding="utf-8")
    print(len(stanzas))
    return 0


def apply(spec_path: str, worktree: str) -> int:
    """Apply one mutation inside `worktree`. Exit 3 if the file did not change."""
    spec = json.loads(pathlib.Path(spec_path).read_text(encoding="utf-8"))
    target = pathlib.Path(worktree) / spec["file"]
    if not target.is_file():
        print(f"target not found in worktree: {target}", file=sys.stderr)
        return 3
    before = target.read_text(encoding="utf-8")

    if "sed" in spec:
        subprocess.run(["sed", "-i", spec["sed"], str(target)], check=True)
    else:
        if spec["old"] not in before:
            return 3
        target.write_text(before.replace(spec["old"], spec.get("new", ""), 1), encoding="utf-8")

    return 0 if target.read_text(encoding="utf-8") != before else 3


def main(argv: list[str]) -> int:
    """Dispatch to parse or apply."""
    if len(argv) < 4:
        print(__doc__, file=sys.stderr)
        return 2
    command, first, second = argv[1], argv[2], argv[3]
    if command == "parse":
        return parse(first, second)
    if command == "apply":
        return apply(first, second)
    print(f"unknown subcommand: {command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
