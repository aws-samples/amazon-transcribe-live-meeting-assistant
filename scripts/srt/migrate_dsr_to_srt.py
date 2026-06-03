#!/usr/bin/env python3
"""One-shot migration of suppressions from ``.dsr/issues.json`` → ``.srt/issues.json``.

DSR is the legacy name for SRT; the file schemas are compatible aside from
two minor cleanups this script applies:

1. Drop entries with ``status: "Open"`` — these are findings, not decisions,
   and SRT will rediscover them on its next scan.
2. Rename ``suppression_reason`` → ``suppressionReason`` (snake_case → camelCase).

Non-destructive: never modifies ``.dsr/issues.json``. Refuses to overwrite an
existing non-empty ``.srt/issues.json`` unless ``--force`` is passed.
"""

import argparse
import json
import sys
from collections import Counter
from pathlib import Path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--force", action="store_true", help="Overwrite an existing .srt/issues.json")
    args = parser.parse_args(argv)

    project_root = Path(__file__).resolve().parent.parent.parent
    dsr_file = project_root / ".dsr" / "issues.json"
    srt_dir = project_root / ".srt"
    srt_file = srt_dir / "issues.json"

    if not dsr_file.exists():
        print(f"❌ {dsr_file.relative_to(project_root)} not found.")
        sys.exit(1)
    if srt_file.exists() and srt_file.stat().st_size > 2 and not args.force:
        print(
            f"⚠️  {srt_file.relative_to(project_root)} already exists. "
            "Re-run with --force to overwrite."
        )
        sys.exit(2)

    with dsr_file.open() as f:
        dsr_issues = json.load(f)
    if not isinstance(dsr_issues, list):
        print(f"❌ Expected a JSON array, got {type(dsr_issues).__name__}.")
        sys.exit(1)

    statuses_in = Counter(issue.get("status", "<missing>") for issue in dsr_issues)
    migrated, dropped, renamed = [], 0, 0
    for issue in dsr_issues:
        if issue.get("status") == "Open":
            dropped += 1
            continue
        new_issue = dict(issue)
        if "suppression_reason" in new_issue:
            if "suppressionReason" not in new_issue:
                new_issue["suppressionReason"] = new_issue.pop("suppression_reason")
                renamed += 1
            else:
                new_issue.pop("suppression_reason")
        migrated.append(new_issue)

    srt_dir.mkdir(exist_ok=True)
    with srt_file.open("w") as f:
        json.dump(migrated, f, indent=2)

    print(f"\nMigrated {len(migrated)} of {len(dsr_issues)} entries → {srt_file.relative_to(project_root)}")
    print(f"  dropped (status='Open'):           {dropped}")
    print(f"  renamed (suppressionReason):       {renamed}")
    print(f"  statuses (in):                     {dict(statuses_in)}")
    print(
        f"  statuses (out):                    "
        f"{dict(Counter(i.get('status', '<missing>') for i in migrated))}"
    )
    print(
        f"  sources (out):                     "
        f"{dict(Counter(i.get('source', '<missing>') for i in migrated))}"
    )
    print("\nNext: make srt-scan && git add .srt/issues.json")


if __name__ == "__main__":
    main()
