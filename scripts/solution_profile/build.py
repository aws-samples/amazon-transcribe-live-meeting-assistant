#!/usr/bin/env python3
# Copyright (c) 2025 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Build the solution-profile release asset (``make solution-profile-build``).

Writes into ``--out-dir``:

* ``solution-profile-<id>-<version>.tar.gz``, with one top-level directory,
  ``solution-profile/``, holding the profile files, the skills and ``build.json``;
  the vendored contract under ``solution-profile/schema/`` is left out;
* ``solution-profile-<id>-<version>.tar.gz.sha256``, in ``sha256sum`` format;
* ``solution-profile-<id>-<version>.build.json``, a copy of the tarball's
  ``build.json`` for a reader that wants the provenance without unpacking.

``source.tag`` and ``source.commit`` are stamped into the BUILT manifest only. The
checked-in manifest never carries them, because a commit cannot name its own hash.

The tar stream is deterministic: entries are sorted, every entry has mtime 0, uid and
gid 0, empty owner names and a fixed mode, the archive is plain USTAR (so no PAX header
can carry a timestamp), and ``build.json`` holds no time. The gzip header has mtime 0
and no file name, so the compressed bytes also match for the same Python and zlib
build; a different zlib can compress the same stream differently.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import re
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import check  # noqa: E402  (the sibling module, found through the path set above)

REPO_ROOT = Path(__file__).resolve().parents[2]
PROFILE_DIR = "solution-profile"
TOP = "solution-profile"
#: The vendored contract lives beside the profile but is not part of the asset.
CONTRACT_DIRS = {"schema"}


def _git(repo_root: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603 - fixed git argv
        ["git", *args],  # noqa: S607 - git from PATH, as the other scripts here use it
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def profile_files(profile_dir: Path) -> list[Path]:
    """The files that go into the asset, relative to ``profile_dir``, sorted."""
    out = []
    for path in sorted(profile_dir.rglob("*")):
        rel = path.relative_to(profile_dir)
        if rel.parts[0] in CONTRACT_DIRS or not path.is_file():
            continue
        if any(part.startswith(".") or part == "__pycache__" for part in rel.parts):
            continue
        out.append(rel)
    return out


def stamp_source(manifest_text: str, tag: str, commit: str) -> str:
    """Insert ``tag`` and ``commit`` under the manifest's ``source:`` mapping.

    A text edit, so comments and key order survive. Refuses a manifest that already
    carries either key: those belong to the built copy only.
    """
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError(f"commit must be a 40-character hex sha, got {commit!r}")
    lines = manifest_text.splitlines(keepends=True)
    try:
        start = next(i for i, line in enumerate(lines) if line.rstrip() == "source:")
    except StopIteration:
        raise ValueError("manifest has no top-level 'source:' mapping") from None
    end = start + 1
    while end < len(lines) and (lines[end].startswith((" ", "\t")) or not lines[end].strip()):
        if re.match(r"\s+(tag|commit):", lines[end]):
            raise ValueError("the checked-in manifest must not carry source.tag or source.commit")
        end += 1
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    stamped = lines[:end] + [f"  tag: {tag}\n", f"  commit: {commit}\n"] + lines[end:]
    result = "".join(stamped)
    source = (yaml.safe_load(result) or {}).get("source") or {}
    if source.get("tag") != tag or source.get("commit") != commit:
        raise ValueError("stamping source.tag/source.commit did not produce the expected mapping")
    return result


def _tarinfo(name: str, size: int | None) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.mtime = 0
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    if size is None:
        info.type = tarfile.DIRTYPE
        info.mode = 0o755
    else:
        info.size = size
        info.mode = 0o644
    return info


def build_tarball(entries: dict[str, bytes]) -> bytes:
    """A deterministic ``.tar.gz`` of ``{relative path: content}`` under ``TOP/``."""
    dirs = {TOP}
    for rel in entries:
        parts = rel.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            dirs.add("/".join([TOP, *parts[:i]]))
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        members: list[tuple[str, bytes | None]] = [(d, None) for d in dirs]
        members += [(f"{TOP}/{rel}", data) for rel, data in entries.items()]
        for name, data in sorted(members, key=lambda m: m[0]):
            if data is None:
                tar.addfile(_tarinfo(name, None))
            else:
                tar.addfile(_tarinfo(name, len(data)), io.BytesIO(data))
    out = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=out, mtime=0, compresslevel=9) as gz:
        gz.write(raw.getvalue())
    return out.getvalue()


def asset_name(profile_id: str, version: str) -> str:
    return f"solution-profile-{profile_id}-{version}"


def build(
    repo_root: Path, out_dir: Path, tag: str | None = None, commit: str | None = None
) -> Path:
    """Write the asset, its ``.sha256`` and ``.build.json`` into ``out_dir``; return the tarball."""
    profile_dir = repo_root / PROFILE_DIR
    manifest_path = profile_dir / "solution-profile.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    profile_id, version = manifest["id"], manifest["version"]
    commit = commit or _git(repo_root, "rev-parse", "HEAD")
    tag = tag or f"v{version}"

    entries: dict[str, bytes] = {}
    for rel in profile_files(profile_dir):
        data = (profile_dir / rel).read_bytes()
        if rel.as_posix() == "solution-profile.yaml":
            data = stamp_source(data.decode("utf-8"), tag, commit).encode("utf-8")
        entries[rel.as_posix()] = data
    build_info = {
        "id": profile_id,
        "version": version,
        "tag": tag,
        "commit": commit,
        "schema_version": manifest["schema_version"],
    }
    build_json = (json.dumps(build_info, indent=2, sort_keys=True) + "\n").encode("utf-8")
    entries["build.json"] = build_json

    archive = build_tarball(entries)
    out_dir.mkdir(parents=True, exist_ok=True)
    name = asset_name(profile_id, version)
    target = out_dir / f"{name}.tar.gz"
    target.write_bytes(archive)
    digest = hashlib.sha256(archive).hexdigest()
    (out_dir / f"{name}.tar.gz.sha256").write_text(f"{digest}  {target.name}\n", encoding="utf-8")
    (out_dir / f"{name}.build.json").write_bytes(build_json)
    return target


def expected_version(repo_root: Path) -> str:
    """The contract form of ``repo_root``'s ``VERSION``."""
    return check.contract_version((repo_root / "VERSION").read_text(encoding="utf-8"))


def validate_unpacked(repo_root: Path, tarball: Path, release: bool, version: str) -> int:
    """Unpack ``tarball`` and run the reference validator over it.

    Evidence paths are relative to the repository at ``source.commit`` and the asset
    holds no repository files, so the validator is pointed at the checkout with
    ``--repo-root``. ``version`` is what the asset should describe, so a tarball built
    from the wrong tree fails here.
    """
    with tempfile.TemporaryDirectory() as tmp:
        # filter="data" refuses absolute paths, ".." members, links out of the target
        # and special files.
        with tarfile.open(tarball, "r:gz") as tar:  # nosemgrep
            tar.extractall(tmp, filter="data")
        unpacked = Path(tmp) / TOP
        version_file = Path(tmp) / "VERSION"
        version_file.write_text(version + "\n", encoding="utf-8")
        return check.run_validator(repo_root, unpacked, version_file, release)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n", 1)[0])
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    parser.add_argument("--out-dir", type=Path, default=Path("dist"))
    parser.add_argument("--tag", help="the release tag (default: v<version>)")
    parser.add_argument("--commit", help="the release commit (default: git rev-parse HEAD)")
    parser.add_argument("--validate", action="store_true", help="validate the unpacked tarball")
    parser.add_argument(
        "--release", action="store_true", help="with --validate, apply the release rules"
    )
    args = parser.parse_args(argv)
    repo_root = args.repo_root.resolve()
    out_dir = args.out_dir if args.out_dir.is_absolute() else repo_root / args.out_dir
    if out_dir.resolve() == (repo_root / PROFILE_DIR).resolve():
        print("refusing to write the asset into the profile directory itself")
        return 2
    target = build(repo_root, out_dir, args.tag, args.commit)
    print(f"wrote {target}")
    print((target.parent / f"{target.name}.sha256").read_text(encoding="utf-8").strip())
    if args.validate:
        return validate_unpacked(repo_root, target, args.release, expected_version(repo_root))
    return 0


if __name__ == "__main__":
    sys.exit(main())
