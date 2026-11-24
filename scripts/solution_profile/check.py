#!/usr/bin/env python3
# Copyright (c) 2025 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Check the solution profile in ``solution-profile/`` (``make solution-profile-check``).

Two halves, and the first is the contract's own:

1. **The reference validator**, vendored unchanged at
   ``solution-profile/schema/v1/validate_profile.py``, run with ``--version-file`` and,
   for a release, ``--release``. It checks every file against the schema, every
   ``<kind>:<id>`` reference, and that every evidence path exists.
2. **Cross-checks against the code**, so the profile cannot quietly fall behind the
   tree. Each derives its universe from the code, and fails, naming the missing item
   and where it belongs, when the code has something the profile does not cover, or
   when the profile still tags something the code no longer has:

   * every tool the hosted MCP server offers (``MCP_TOOLS`` in ``mcp_analytics/index.py``
     and ``MEETING_TOOLS`` in ``mcp_analytics/tool_definitions.py``) is tagged
     ``mcp-tool:<name>`` on a capability;
   * every platform the Virtual Participant accepts (the values of ``_PLATFORMS`` in
     ``mcp_analytics/tools/platform_helper.py``) is tagged ``vp-platform:<VALUE>``;
   * every shipped configuration preset (``lma_config/presets/<id>.json``) is tagged
     ``preset:<id>``;
   * the ``cloudformation`` deployment option's ``regions`` are the Regions
     ``aws-release.sh`` publishes to;
   * the latest ten releases in ``CHANGELOG.md`` each have a ``changes.yaml`` entry,
     and one whose section carries an action item records at least one;
   * every ``<file>.md#<anchor>`` evidence entry names a heading that file has (the
     validator checks the path, not the anchor).

**The version.** The contract's version pattern is ``X.Y.Z`` with an optional
``-suffix`` or ``+suffix``. ``VERSION`` holds a PEP 440 version, which on ``develop`` is
a development build such as ``0.4.1.dev1`` that the pattern rejects. So the validator
is given the contract form (``0.4.1-dev1``) in a temporary file; a final release is
already in contract form and ``VERSION`` itself is passed. ``make version`` writes the
same conversion into the manifest (``--stamp-version``), so the two cannot disagree.

**Release rules.** ``--release`` is applied when ``VERSION`` is a final release
(``X.Y.Z``), when ``--release`` is given, or when ``SOLUTION_PROFILE_RELEASE`` is set to
an affirmative value. The release workflow sets it.

Needs PyYAML, jsonschema and nothing else; every universe is read from the
``--repo-root`` given, never from the checkout this script happens to live in.
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import os
import re
import sys
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
PROFILE_DIR = "solution-profile"
VALIDATOR = "solution-profile/schema/v1/validate_profile.py"
MCP_DIR = "lma-ai-stack/source/lambda_functions/mcp_analytics"
MCP_INDEX = f"{MCP_DIR}/index.py"
MCP_MEETING_TOOLS = f"{MCP_DIR}/tool_definitions.py"
PLATFORM_HELPER = f"{MCP_DIR}/tools/platform_helper.py"
PRESETS_DIR = "lma-ai-stack/source/lambda_layers/configuration_layer/lma_config/presets"
RELEASE_SCRIPT = "aws-release.sh"
CHANGELOG = "CHANGELOG.md"
REVIEW_SKILL = ".claude/skills/prepare-changelog.md"

#: The files the cross-checks read, beside the profile and its evidence. The CI step
#: selection (scripts/ci/changed_areas.py) runs the check when any of them changes.
CROSS_CHECK_SOURCES = (
    MCP_INDEX,
    MCP_MEETING_TOOLS,
    PLATFORM_HELPER,
    PRESETS_DIR,
    RELEASE_SCRIPT,
    CHANGELOG,
    "VERSION",
)

#: How many of the newest CHANGELOG releases changes.yaml must cover. The contract
#: says "keep at least the last ten releases".
RELEASES_COVERED = 10

PEP440 = re.compile(r"^(\d+\.\d+\.\d+)(?:\.?([0-9A-Za-z][0-9A-Za-z.]*))?$")
AFFIRMATIVE = {"1", "true", "yes", "y", "on"}


# ---------------------------------------------------------------------------
# Version
# ---------------------------------------------------------------------------


def contract_version(pep440: str) -> str:
    """The contract's form of a PEP 440 version: ``0.4.1.dev1`` -> ``0.4.1-dev1``.

    A final release is returned unchanged. Raises ``ValueError`` for anything that is
    not ``X.Y.Z`` optionally followed by a suffix.
    """
    match = PEP440.match(pep440.strip())
    if not match:
        raise ValueError(f"not an X.Y.Z[suffix] version: {pep440.strip()!r}")
    base, suffix = match.groups()
    return base if not suffix else f"{base}-{suffix}"


def is_final_release(pep440: str) -> bool:
    """True for ``X.Y.Z`` with no development, pre-release or post-release suffix."""
    return contract_version(pep440) == pep440.strip()


def stamp_manifest_version(manifest: Path, pep440: str) -> bool:
    """Rewrite the manifest's top-level ``version:`` line; return whether it changed.

    A text edit rather than a YAML round trip, so comments and key order survive.
    """
    text = manifest.read_text(encoding="utf-8")
    new, count = re.subn(
        r"^version: .*$", f"version: {contract_version(pep440)}", text, flags=re.MULTILINE
    )
    if count != 1:
        raise ValueError(f"{manifest}: expected one top-level 'version:' line, found {count}")
    manifest.write_text(new, encoding="utf-8")
    return new != text


# ---------------------------------------------------------------------------
# Universes derived from the code
# ---------------------------------------------------------------------------


def _module_assignment(path: Path, name: str) -> ast.expr:
    """The value assigned to the module-level ``name`` in ``path``."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets = [node.target]
        if any(isinstance(t, ast.Name) and t.id == name for t in targets):
            value = node.value
            assert value is not None
            return value
    raise RuntimeError(f"{path}: no module-level {name}")


def _tool_names(path: Path, name: str) -> set[str]:
    """The ``"name"`` of each dict literal in the list assigned to ``name``.

    Read from the AST, so the module is not imported (it needs boto3 and the Lambda
    environment) and tools whose other keys use named constants are still seen.
    """
    value = _module_assignment(path, name)
    if not isinstance(value, ast.List):
        raise RuntimeError(f"{path}: {name} is not a list literal")
    names: set[str] = set()
    for element in value.elts:
        if not isinstance(element, ast.Dict):
            raise RuntimeError(f"{path}: an entry of {name} is not a dict literal")
        for key, item in zip(element.keys, element.values):
            if (
                isinstance(key, ast.Constant)
                and key.value == "name"
                and isinstance(item, ast.Constant)
                and isinstance(item.value, str)
            ):
                names.add(item.value)
    if not names:
        raise RuntimeError(f"{path}: {name} names no tools")
    return names


def mcp_tools(repo_root: Path) -> set[str]:
    """Every tool the hosted MCP server offers, on either endpoint."""
    return _tool_names(repo_root / MCP_INDEX, "MCP_TOOLS") | _tool_names(
        repo_root / MCP_MEETING_TOOLS, "MEETING_TOOLS"
    )


def vp_platforms(repo_root: Path) -> set[str]:
    """Every platform value the Virtual Participant accepts (``_PLATFORMS`` values)."""
    table = ast.literal_eval(_module_assignment(repo_root / PLATFORM_HELPER, "_PLATFORMS"))
    if not isinstance(table, dict) or not table:
        raise RuntimeError(f"{PLATFORM_HELPER}: _PLATFORMS is not a non-empty dict")
    return {str(v) for v in table.values()}


def preset_names(repo_root: Path) -> set[str]:
    """Every shipped configuration preset: ``presets/<id>.json``."""
    root = repo_root / PRESETS_DIR
    names = {p.stem for p in root.glob("*.json")}
    if not names:
        raise RuntimeError(f"{PRESETS_DIR}: no presets found")
    return names


def release_regions(repo_root: Path) -> set[str]:
    """The Regions ``aws-release.sh`` publishes to: the third argument of publish.sh."""
    regions: set[str] = set()
    for line in (repo_root / RELEASE_SCRIPT).read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("#"):
            continue
        regions.update(re.findall(r"publish\.sh\s+\S+\s+\S+\s+([a-z0-9-]+)", line))
    if not regions:
        raise RuntimeError(f"{RELEASE_SCRIPT}: no publish.sh invocations found")
    return regions


def changelog_releases(repo_root: Path) -> list[tuple[str, str]]:
    """``(version, section text)`` for each ``## [x.y.z]`` section, newest first."""
    text = (repo_root / CHANGELOG).read_text(encoding="utf-8")
    heads = list(re.finditer(r"^## \[(\d+\.\d+\.\d+)\][^\n]*$", text, flags=re.MULTILINE))
    out = []
    for i, head in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        out.append((head.group(1), text[head.end() : end]))
    return out


#: How this CHANGELOG marks something a user upgrading must act on.
ACTION_MARKERS = re.compile(r"⚠️|\*\*Action required|\*\*Updating from")


def has_action_item(section: str) -> bool:
    """Whether a CHANGELOG release section carries an item a user must act on."""
    return ACTION_MARKERS.search(section) is not None


# ---------------------------------------------------------------------------
# The checks
# ---------------------------------------------------------------------------


@dataclass
class Profile:
    """The parsed profile files a cross-check reads."""

    capabilities: list[dict[str, Any]] = field(default_factory=list)
    deployment_options: list[dict[str, Any]] = field(default_factory=list)
    releases: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def load(cls, profile_dir: Path) -> Profile:
        import yaml  # here, so --stamp-version (make version) needs no PyYAML

        def read(name: str, key: str) -> list[dict[str, Any]]:
            path = profile_dir / name
            if not path.is_file():
                return []
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            return [item for item in data.get(key) or [] if isinstance(item, dict)]

        return cls(
            capabilities=read("capabilities.yaml", "capabilities"),
            deployment_options=read("deployment.yaml", "options"),
            releases=read("changes.yaml", "releases"),
        )


def _tagged(items: Iterable[dict[str, Any]], prefix: str) -> dict[str, list[str]]:
    """``value -> [item ids]`` for every ``<prefix><value>`` tag on ``items``."""
    out: dict[str, list[str]] = {}
    for item in items:
        for tag in item.get("tags") or []:
            if isinstance(tag, str) and tag.startswith(prefix):
                out.setdefault(tag[len(prefix) :], []).append(str(item.get("id")))
    return out


def _check_tagged(
    profile: Profile, universe: set[str], prefix: str, source: str, what: str, hint: str
) -> list[str]:
    tagged = _tagged(profile.capabilities, prefix)
    problems = [
        f"capabilities.yaml: no capability is tagged '{prefix}{name}'. {source} has {what} "
        f"'{name}'; {hint}"
        for name in sorted(universe - set(tagged))
    ]
    problems += [
        f"capabilities.yaml: {tagged[name]} tagged '{prefix}{name}', which {source} no longer "
        f"has; fix or remove the tag"
        for name in sorted(set(tagged) - universe)
    ]
    return problems


def check_mcp_tools(profile: Profile, universe: set[str]) -> list[str]:
    return _check_tagged(
        profile,
        universe,
        "mcp-tool:",
        f"{MCP_INDEX} or {MCP_MEETING_TOOLS}",
        "the MCP tool",
        "tag it on the capability the tool serves (and on mcp-server-oauth-gateway if the "
        "OAuth endpoint offers it)",
    )


def check_platforms(profile: Profile, universe: set[str]) -> list[str]:
    return _check_tagged(
        profile,
        universe,
        "vp-platform:",
        PLATFORM_HELPER,
        "the Virtual Participant platform",
        "add a vp-<platform> capability for it with that tag, and cover its limits in "
        "limitations.yaml",
    )


def check_presets(profile: Profile, universe: set[str]) -> list[str]:
    return _check_tagged(
        profile,
        universe,
        "preset:",
        PRESETS_DIR,
        "the configuration preset",
        "tag it on capability configuration-presets and name it in that description",
    )


def check_regions(profile: Profile, universe: set[str]) -> list[str]:
    option = next((o for o in profile.deployment_options if o.get("id") == "cloudformation"), None)
    if option is None:
        return ["deployment.yaml: no option with id 'cloudformation'"]
    listed = set(option.get("regions") or [])
    problems = []
    if universe - listed:
        problems.append(
            f"deployment.yaml: cloudformation: regions lack {sorted(universe - listed)}, which "
            f"{RELEASE_SCRIPT} publishes a release to"
        )
    if listed - universe:
        problems.append(
            f"deployment.yaml: cloudformation: regions list {sorted(listed - universe)}, which "
            f"{RELEASE_SCRIPT} does not publish to"
        )
    return problems


def check_changes(profile: Profile, releases: list[tuple[str, str]]) -> list[str]:
    entries = {str(r.get("version")): r for r in profile.releases}
    problems = []
    for version, section in releases[:RELEASES_COVERED]:
        entry = entries.get(version)
        if entry is None:
            problems.append(
                f"changes.yaml: no entry for {version}, one of the latest {RELEASES_COVERED} "
                f"releases in {CHANGELOG}"
            )
        elif has_action_item(section) and not entry.get("action_required"):
            problems.append(
                f"changes.yaml: {version}: {CHANGELOG} marks an item users must act on in this "
                f"release, but action_required is empty"
            )
    return problems


def slug(heading: str) -> str:
    """The anchor GitHub and the docs site give a heading (github-slugger's rule)."""
    text = heading.strip().lower().replace("`", "")
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def anchors(markdown: str) -> set[str]:
    """Every heading anchor in ``markdown``, with github-slugger's ``-1``, ``-2`` suffixes."""
    seen: dict[str, int] = {}
    out: set[str] = set()
    in_fence = False
    for line in markdown.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        match = None if in_fence else re.match(r"^#{1,6}\s+(.*?)\s*#*\s*$", line)
        if not match:
            continue
        base = slug(match.group(1))
        count = seen.get(base, 0)
        out.add(base if count == 0 else f"{base}-{count}")
        seen[base] = count + 1
    return out


def _evidence(node: Any, item_id: str = "") -> Iterable[tuple[str, str]]:
    """Yield ``(enclosing item id, evidence entry)`` for every ``evidence`` list."""
    if isinstance(node, dict):
        here = str(node.get("id", item_id))
        for key, value in node.items():
            if key == "evidence" and isinstance(value, list):
                yield from ((here, v) for v in value if isinstance(v, str))
            else:
                yield from _evidence(value, here)
    elif isinstance(node, list):
        for item in node:
            yield from _evidence(item, item_id)


def evidence_entries(profile_dir: Path) -> list[tuple[str, str, str]]:
    """``(file name, item id, entry)`` for every evidence entry in the profile."""
    import yaml

    out = []
    for path in sorted(profile_dir.glob("*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        out += [(path.name, item_id, entry) for item_id, entry in _evidence(data)]
    return out


def check_evidence_anchors(repo_root: Path, profile_dir: Path) -> list[str]:
    """Every ``<path>.md#<anchor>`` evidence entry names a heading that file has."""
    problems = []
    cache: dict[str, set[str]] = {}
    for name, item_id, entry in evidence_entries(profile_dir):
        if entry.startswith("https://") or "#" not in entry:
            continue
        rel, fragment = entry.split("#", 1)
        if not rel.endswith(".md") or not (repo_root / rel).is_file():
            continue
        if rel not in cache:
            cache[rel] = anchors((repo_root / rel).read_text(encoding="utf-8"))
        if fragment not in cache[rel]:
            problems.append(
                f"{name}: {item_id}: evidence anchor '#{fragment}' matches no heading in {rel}"
            )
    return problems


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------

Universe = Callable[[Path], Any]
CROSS_CHECKS: list[tuple[str, Universe, Callable[[Profile, Any], list[str]]]] = [
    ("MCP tools", mcp_tools, check_mcp_tools),
    ("Virtual Participant platforms", vp_platforms, check_platforms),
    ("configuration presets", preset_names, check_presets),
    ("release Regions", release_regions, check_regions),
    ("releases", changelog_releases, check_changes),
]


def cross_check(repo_root: Path, profile_dir: Path) -> list[str]:
    """Every cross-check's problems, in order."""
    profile = Profile.load(profile_dir)
    problems: list[str] = []
    for _name, derive, check in CROSS_CHECKS:
        problems += check(profile, derive(repo_root))
    return problems + check_evidence_anchors(repo_root, profile_dir)


def load_validator(repo_root: Path) -> Any:
    """The vendored reference validator, loaded from its file."""
    path = repo_root / VALIDATOR
    spec = importlib.util.spec_from_file_location("_validate_profile", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_validator(repo_root: Path, profile_dir: Path, version_file: Path, release: bool) -> int:
    """Run the reference validator's own ``main`` on the profile; return its status."""
    argv = [str(profile_dir), "--repo-root", str(repo_root), "--version-file", str(version_file)]
    if release:
        argv.append("--release")
    status = load_validator(repo_root).main(argv)
    sys.stdout.flush()
    return int(status)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n", 1)[0])
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    parser.add_argument("--profile-dir", type=Path, help="default: <repo-root>/solution-profile")
    parser.add_argument("--release", action="store_true", help="apply the contract's release rules")
    parser.add_argument("--contract-only", action="store_true", help="run the validator only")
    parser.add_argument(
        "--stamp-version",
        metavar="PEP440",
        help="write this version, in contract form, into the manifest and exit (make version)",
    )
    args = parser.parse_args(argv)
    repo_root: Path = args.repo_root.resolve()
    profile_dir: Path = (args.profile_dir or repo_root / PROFILE_DIR).resolve()

    if args.stamp_version:
        try:
            changed = stamp_manifest_version(
                profile_dir / "solution-profile.yaml", args.stamp_version
            )
        except ValueError as exc:
            print(f"ERROR: could not stamp the solution profile version: {exc}")
            return 1
        suffix = "" if changed else " (unchanged)"
        print(
            f"  solution-profile/solution-profile.yaml: {contract_version(args.stamp_version)}{suffix}"
        )
        return 0

    pep440 = (repo_root / "VERSION").read_text(encoding="utf-8").strip()
    try:
        version = contract_version(pep440)
    except ValueError as exc:
        print(f"VERSION: {exc}")
        return 1
    release = (
        args.release
        or os.environ.get("SOLUTION_PROFILE_RELEASE", "").strip().lower() in AFFIRMATIVE
        or is_final_release(pep440)
    )
    state = "ON" if release else "off (development version)"
    print(
        f"Solution profile: VERSION {pep440} -> profile version {version}; release rules {state}",
        flush=True,
    )

    with tempfile.TemporaryDirectory() as tmp:
        if version == pep440:
            version_file = repo_root / "VERSION"
        else:
            version_file = Path(tmp) / "VERSION"
            version_file.write_text(version + "\n", encoding="utf-8")
        status = run_validator(repo_root, profile_dir, version_file, release)
    if status != 0 and release:
        print(
            f"\nThis is a release build (VERSION {pep440}). Follow 'Review the solution profile' "
            f"in {REVIEW_SKILL}: review the curated files in {PROFILE_DIR}/ against the "
            f"CHANGELOG section for {pep440}, add a changes.yaml entry for {version}, and set "
            f"reviewed_for_version: {version} in {PROFILE_DIR}/solution-profile.yaml."
        )
    if args.contract_only:
        return status

    problems = cross_check(repo_root, profile_dir)
    for problem in problems:
        print(problem)
    print("cross-checks: " + ("passed" if not problems else f"{len(problems)} problem(s)"))
    return 1 if status != 0 or problems else 0


if __name__ == "__main__":
    sys.exit(main())
