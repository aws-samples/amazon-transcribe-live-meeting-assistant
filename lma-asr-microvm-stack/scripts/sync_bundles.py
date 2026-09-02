#!/usr/bin/env python3
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Regenerate the CloudFormation bundle lists from ``source/catalog.json``.

Adding a bundle otherwise means hand-editing the same two facts into this stack's
``template.yaml``: the ``AsrModelBundle`` allowed values and the ``BundleMemory``
mapping. Both duplications are already guarded by tests, but a guard that fails
after the fact is worse than not having to edit by hand at all -- an earlier
character-offset edit to these blocks silently deleted four unrelated parameters,
and only cfn-lint caught it.

``lma-main.yaml`` used to be rewritten here too, because it declared its own
``AsrModelBundle`` parameter. It no longer does: the MicroVM ASR tuning parameters
were withdrawn from the deploy-time surface while the engine is experimental, and
the chosen bundle now lives in that template's ``AsrDefaults`` mapping. There is no
list to generate there any more -- one value -- but it is still a hand-edited copy
of a catalog id, so it is *validated* instead (see ``check_main_template``). Dropping
the file from ``TEMPLATES`` without that check would have quietly lost the only
thing stopping ``lma-main.yaml`` from naming a bundle that does not exist, which
fails 20 minutes into a deploy rather than at commit time.

The memory duplication cannot be removed: ``MinimumMemoryInMiB`` needs a
CloudFormation-typed number and a Mapping cannot be keyed on a value a custom
resource resolved. So it is generated instead.

Usage::

    python3 scripts/sync_bundles.py            # rewrite in place
    python3 scripts/sync_bundles.py --check     # exit 1 if out of sync (for CI)

Run from the repository root or from this stack's directory; paths are resolved
relative to this file.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

STACK_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = STACK_DIR.parent
CATALOG = STACK_DIR / "source" / "catalog.json"
TEMPLATES = (STACK_DIR / "template.yaml",)
MAIN_TEMPLATE = REPO_ROOT / "lma-main.yaml"

PARAM = "AsrModelBundle"
MAPPING = "BundleMemory"

# The single bundle id in lma-main.yaml's AsrDefaults mapping, e.g.
#       ModelBundle: nemotron-titanet-small
MAIN_BUNDLE_RE = re.compile(r"^ {6}ModelBundle: *(\S+) *$", re.MULTILINE)


class SyncError(RuntimeError):
    """The templates could not be rewritten safely."""


def bundles() -> tuple[list[str], dict[str, int]]:
    catalog = json.loads(CATALOG.read_text())
    entries = catalog.get("bundles") or []
    if not entries:
        raise SyncError(f"{CATALOG} defines no bundles")
    ids = [entry["id"] for entry in entries]
    if len(set(ids)) != len(ids):
        raise SyncError(f"duplicate bundle ids: {ids}")
    default = catalog.get("defaultBundleId")
    if default not in ids:
        raise SyncError(f"defaultBundleId {default!r} is not one of {ids}")
    memory = {entry["id"]: int(entry.get("baselineMemoryMiB", 8192)) for entry in entries}
    return ids, memory


def _top_level_keys(lines: list[str]) -> int:
    return len([line for line in lines if re.match(r"^  [A-Za-z]", line)])


def rewrite_allowed_values(lines: list[str], ids: list[str]) -> list[str]:
    """Replace only the AllowedValues list inside the bundle parameter.

    Line-based and bounded by indentation, so no following parameter can be consumed
    however the surrounding YAML is formatted.
    """
    out: list[str] = []
    index = 0
    while index < len(lines) and lines[index] != f"  {PARAM}:\n":
        out.append(lines[index])
        index += 1
    if index == len(lines):
        raise SyncError(f"{PARAM} not found")
    out.append(lines[index])
    index += 1

    replaced = False
    while index < len(lines):
        line = lines[index]
        if not line.startswith("    ") and line.strip():
            break  # end of this parameter
        if line == "    AllowedValues:\n":
            out.append(line)
            index += 1
            while index < len(lines) and lines[index].startswith("      - "):
                index += 1
            out.extend(f"      - {bundle}\n" for bundle in ids)
            replaced = True
            continue
        out.append(line)
        index += 1
    if not replaced:
        raise SyncError(f"{PARAM} has no AllowedValues block")
    out.extend(lines[index:])
    return out


def rewrite_mapping(lines: list[str], memory: dict[str, int], ids: list[str]) -> list[str]:
    marker = f"  {MAPPING}:\n"
    if marker not in lines:
        raise SyncError(f"{MAPPING} mapping not found")
    index = lines.index(marker)
    out = lines[: index + 1]
    index += 1
    while index < len(lines) and lines[index].startswith("    "):
        index += 1
    for bundle in ids:
        out.append(f"    {bundle}:\n")
        out.append(f"      MiB: {memory[bundle]}\n")
    out.extend(lines[index:])
    return out


def check_main_template(ids: list[str]) -> None:
    """Assert lma-main.yaml's AsrDefaults mapping names a bundle the catalog ships.

    Nothing is generated there -- the mapping holds one id, not a list -- but it is
    still a hand-maintained copy of a catalog value, and a typo or a renamed bundle
    would only surface when the ASR stack rejected the parameter partway through a
    deploy.
    """
    text = MAIN_TEMPLATE.read_text()
    found = MAIN_BUNDLE_RE.findall(text)
    if not found:
        raise SyncError(
            f"{MAIN_TEMPLATE.name}: no 'ModelBundle:' entry found in the AsrDefaults "
            "mapping (was the mapping renamed or reindented?)"
        )
    if len(found) > 1:
        raise SyncError(f"{MAIN_TEMPLATE.name}: expected one ModelBundle entry, found {found}")
    if found[0] not in ids:
        raise SyncError(
            f"{MAIN_TEMPLATE.name}: AsrDefaults ModelBundle is {found[0]!r}, which is not "
            f"a bundle in catalog.json ({', '.join(ids)})"
        )


def sync(check: bool) -> int:
    ids, memory = bundles()
    check_main_template(ids)
    stale: list[Path] = []

    for path in TEMPLATES:
        original = path.read_text().splitlines(keepends=True)
        updated = rewrite_allowed_values(original, ids)
        if MAPPING + ":\n" in "".join(updated) or f"  {MAPPING}:\n" in updated:
            updated = rewrite_mapping(updated, memory, ids)

        before, after = _top_level_keys(original), _top_level_keys(updated)
        if before != after:
            raise SyncError(
                f"{path.name}: rewriting changed the top-level key count "
                f"({before} -> {after}); refusing to write"
            )

        if updated == original:
            continue
        stale.append(path)
        if not check:
            path.write_text("".join(updated))

    if check:
        for path in stale:
            print(f"out of sync with catalog.json: {path}", file=sys.stderr)
        if stale:
            print("run: python3 lma-asr-microvm-stack/scripts/sync_bundles.py", file=sys.stderr)
            return 1
        print(f"templates match catalog.json ({len(ids)} bundles)")
        return 0

    for path in stale:
        print(f"updated {path}")
    if not stale:
        print(f"already in sync ({len(ids)} bundles)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="report drift and exit non-zero instead of rewriting",
    )
    args = parser.parse_args()
    try:
        return sync(args.check)
    except SyncError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
