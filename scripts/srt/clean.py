#!/usr/bin/env python3
"""Clean LMA-side build artifacts before an SRT scan.

SRT v1.x invokes Bandit / Semgrep / Checkov / Syft with a hardcoded set of
``--exclude`` paths that does NOT include the vendored Lambda-layer trees,
the SAM build output, or the SAM-packaged ``out/`` directories.  Without
those excludes Bandit grinds through hundreds of thousands of lines of
vendored boto3 / strands / opensearchpy code, inflates the HIGH/CRITICAL
finding count with false positives from third-party libraries, and (on
slower hosts) blows past the SRT assess timeout.

Rather than try to teach SRT about our project layout, this script
*physically removes* the noise sources before each scan.  Everything
deleted is already declared ignored in ``.gitignore`` and is regenerated
on demand by:

  * ``cd <stack> && make`` or ``./publish.sh`` (rebuilds layer/python/,
    .aws-sam/, out/)
  * ``npm install`` (regenerates node_modules/)
  * ``ash`` (regenerates .ash/)

Items deliberately **NOT** removed (because regenerating them is slow or
breaks local state):

  * ``.srt/srt``, ``.srt/.venv``, ``.srt/srt-cli-*.tar.gz``  → SRT binary
    and scanner toolchain (re-installed by ``make srt-setup``, ~5 min)
  * ``.srt/issues.json`` (suppression DB — committed)
  * ``.srt/srtconfig.json``, ``.srt/settings.json``  → SRT config
  * ``.dsr/issues.json``                              → legacy DSR DB
  * ``.venv/``                                        → root project venv
  * ``**/.checksum``                                  → build cache (use
    ``make srt-clean-checksums`` to wipe these separately)
  * ``lma-virtual-participant-stack/backend/.env.local``  → local VP dev

Usage:
    python3 scripts/srt/clean.py         # show what would be removed
    python3 scripts/srt/clean.py --apply # actually remove
    make srt-clean                       # convenience wrapper for --apply
"""

from __future__ import annotations

import argparse
import fnmatch
import os
import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# Directory basenames whose mere presence should be removed wherever they
# occur.  Walked top-down with pruning, so we never recurse INTO a matched
# directory (avoids the O(n*m) tarpit you hit with ``Path("**/node_modules")``
# scanning ``node_modules/.../node_modules/...``).
REMOVE_DIR_BASENAMES: set[str] = {
    # SAM build / package artifacts.
    ".aws-sam",
    "out",
    # Compiled JS / TS bundles and NPM trees.
    "dist",
    "build",
    "node_modules",
    # Python bytecode caches.
    "__pycache__",
    # ASH workspace from prior scans.
    ".ash",
}

# (parent_basename, child_basename) tuples for vendored Lambda-layer
# pip-installed trees: e.g. ``lma-meetingassist-setup-stack/boto3_layer/python``.
# We match on the *parent* directory's name + the literal child ``python``
# so we don't accidentally nuke unrelated directories named ``python``.
REMOVE_LAYER_PYTHON_PARENTS: set[str] = {
    "boto3_layer",
    "strands_layer",
    "opensearchpy_layer",
    "transcript_enrichment_layer",
}

# File-glob patterns (matched per filename via fnmatch on os.walk's file list).
REMOVE_FILE_GLOBS: tuple[str, ...] = (
    "*.pyc",
    "boto3_lambda_layer.zip",
    "strands_layer.zip",
    "transcriber-layer.zip",
)

# Specific paths (relative to PROJECT_ROOT) that should always be removed
# verbatim.  These are intermediate scan outputs that don't fit the
# ``REMOVE_DIR_BASENAMES`` rule (we don't want to wipe every ``logs/`` or
# every file named ``dashboard.html`` repo-wide).
REMOVE_EXPLICIT: tuple[str, ...] = (
    ".ash",
    ".srt/bandit-scan.json",
    ".srt/bandit-summary.json",
    ".srt/semgrep-scan.json",
    ".srt/semgrep-summary.json",
    ".srt/syft-scan.json",
    ".srt/syft-summary.json",
    ".srt/dashboard.html",
    ".srt/project-summary.md",
    ".srt/logs",
    "lma-browser-extension-stack/dist-chrome",
    "lma-browser-extension-stack/dist-firefox",
)

# Glob patterns relative to ``.srt/`` for per-template scan workspace dirs.
REMOVE_SRT_CHILD_GLOBS: tuple[str, ...] = (
    ".ash-*",
    "lma-*-stack-*",
    "lma-main",
    "iam-roles-*",
)


# Anything matching one of these (relative to PROJECT_ROOT) is never
# removed, even if a glob above would otherwise pick it up.  Items here
# are either committed (issues.json) or expensive to regenerate (the SRT
# 80 MB binary + scanner venv).
PRESERVE_RELATIVE: set[str] = {
    ".srt/srt",
    ".srt/srtconfig.json",
    ".srt/settings.json",
    ".srt/issues.json",
    ".srt/.venv",
    ".dsr/issues.json",
    ".dsr/settings.json",
    ".dsr/feature-security-review-v0.1.0-to-v0.3.2.md",
    ".venv",
    "lma-virtual-participant-stack/backend/.env.local",
}


def _human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def _path_size(p: Path) -> int:
    if p.is_file() or p.is_symlink():
        try:
            return p.stat().st_size
        except OSError:
            return 0
    total = 0
    for root, _dirs, files in os.walk(p, followlinks=False):
        for f in files:
            try:
                total += (Path(root) / f).stat().st_size
            except OSError:
                pass
    return total


def _is_preserved(p: Path) -> bool:
    """Return True if ``p`` (or any ancestor) is in ``PRESERVE_RELATIVE``."""
    rel = p.relative_to(PROJECT_ROOT)
    rel_str = str(rel)
    if rel_str in PRESERVE_RELATIVE:
        return True
    parts = rel.parts
    for i in range(1, len(parts) + 1):
        if "/".join(parts[:i]) in PRESERVE_RELATIVE:
            return True
    return False


# Top-level entries we never descend into during the walk.  These are dirs
# whose contents we want to keep verbatim (so we shouldn't even traverse
# them looking for ``__pycache__``/``dist`` etc.).
WALK_PRUNE_TOP_LEVEL: set[str] = {
    ".git",
    ".srt",   # walked separately so we can preserve binary/venv
    ".dsr",   # legacy; preserved by negative-gitignore
    ".venv",  # root project venv
}


def _gather_targets() -> list[Path]:
    """Return the de-duplicated list of paths to remove.

    Implementation notes:
      * Uses ``os.walk(topdown=True)`` and prunes ``dirnames`` in-place so we
        never descend into a directory that's already a removal target
        (e.g. once we hit ``node_modules`` we don't traverse its 200k files).
      * Layer ``python`` dirs are matched on the parent's basename so we
        only flag the vendored Lambda-layer trees, not arbitrary
        ``foo/python/`` directories.
    """
    seen: set[Path] = set()
    targets: list[Path] = []

    def add(p: Path) -> None:
        if p in seen or _is_preserved(p):
            return
        seen.add(p)
        targets.append(p)

    # 1. Walk the tree once with pruning.
    for dirpath, dirnames, filenames in os.walk(PROJECT_ROOT, topdown=True):
        dp = Path(dirpath)

        # Skip preserved subtrees entirely.
        if _is_preserved(dp):
            dirnames[:] = []
            continue

        # Prune top-level dirs we never descend into for this walk.
        if dp == PROJECT_ROOT:
            dirnames[:] = [d for d in dirnames if d not in WALK_PRUNE_TOP_LEVEL]

        # Prune-and-collect: any child dir whose basename matches a
        # removal rule is added as a target and NOT descended into.
        keep_dirs: list[str] = []
        for d in dirnames:
            child = dp / d
            if d in REMOVE_DIR_BASENAMES:
                add(child)
                continue
            # Vendored Lambda-layer ``<layer>/python/`` trees.
            if d == "python" and dp.name in REMOVE_LAYER_PYTHON_PARENTS:
                add(child)
                continue
            keep_dirs.append(d)
        dirnames[:] = keep_dirs

        # Files to remove inside dirs we DID keep.
        for fname in filenames:
            for glob in REMOVE_FILE_GLOBS:
                if fnmatch.fnmatch(fname, glob):
                    add(dp / fname)
                    break

    # 2. Add explicit paths.
    for rel in REMOVE_EXPLICIT:
        p = PROJECT_ROOT / rel
        if p.exists() or p.is_symlink():
            add(p)

    # 3. ``.srt`` per-template subdirs (selectively — we don't walk .srt
    #    above so we don't accidentally remove the binary/venv).
    srt_dir = PROJECT_ROOT / ".srt"
    if srt_dir.is_dir():
        for child in srt_dir.iterdir():
            for glob in REMOVE_SRT_CHILD_GLOBS:
                if fnmatch.fnmatch(child.name, glob):
                    add(child)
                    break

    # Sort longest path first so we delete leaves before their parents.
    targets.sort(key=lambda x: len(str(x)), reverse=True)
    return targets



def _remove(p: Path) -> None:
    if p.is_symlink() or p.is_file():
        p.unlink(missing_ok=True)
    elif p.is_dir():
        shutil.rmtree(p, ignore_errors=False)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", maxsplit=1)[0])
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually delete the matched paths. Without this flag, only print what would be removed.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress per-path output; print only the summary line.",
    )
    args = parser.parse_args()

    targets = _gather_targets()
    if not targets:
        print("✅ Nothing to clean — repository is already pristine for an SRT scan.")
        return 0

    total_bytes = 0
    for p in targets:
        sz = _path_size(p)
        total_bytes += sz
        if not args.quiet:
            verb = "Removing" if args.apply else "Would remove"
            kind = "/" if p.is_dir() and not p.is_symlink() else ""
            rel = p.relative_to(PROJECT_ROOT)
            print(f"  {verb}: {rel}{kind}  ({_human_size(sz)})")

    summary_verb = "Removed" if args.apply else "Would remove"
    print(
        f"\n{summary_verb} {len(targets)} path(s) totaling {_human_size(total_bytes)}."
    )

    if not args.apply:
        print("\nRe-run with --apply (or `make srt-clean`) to actually delete.")
        return 0

    errors = 0
    for p in targets:
        try:
            _remove(p)
        except OSError as exc:  # noqa: BLE001
            errors += 1
            print(f"  ⚠️  Could not remove {p}: {exc}", file=sys.stderr)

    if errors:
        print(f"\n⚠️  Completed with {errors} error(s).", file=sys.stderr)
        return 1

    print("\n✅ Clean complete. Run `make srt-scan` next.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
