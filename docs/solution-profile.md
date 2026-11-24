---
title: "Solution Profile"
---

<!-- Copyright (c) 2025 Amazon.com. This file is licensed under the MIT-0 License. -->
<!-- See the LICENSE file in the project root for full license information. -->

# Solution Profile

LMA publishes a **solution profile** with every release: a small, versioned,
machine-readable description of what LMA does, where it can be extended, where it
stops, the questions that test whether it fits a need, and how a deployment usually
starts. It is meant for AI assistants and for people who judge, early in a project,
whether LMA fits and what would have to be built around it. It is written against
version 1 of the Solution Profile contract, which other solutions publish profiles
under too, so one reader can compare them.

The profile is built from the source at each release tag, so it describes exactly
that release, and it is attached to the release on GitHub.

## Contents

- [What is in the profile](#what-is-in-the-profile)
- [Getting the profile for a release](#getting-the-profile-for-a-release)
- [The checks](#the-checks)
- [Publishing](#publishing)
- [Reviewing the profile for a release](#reviewing-the-profile-for-a-release)
- [The vendored contract](#the-vendored-contract)

## What is in the profile

The profile is the [`solution-profile/`](../solution-profile/) directory at the
repository root:

| File | What it holds |
|---|---|
| `solution-profile.yaml` | The manifest: id `live-meeting-assistant`, version, summary, the keywords and phrases that suggest a need LMA serves (and those that suggest it does not), the licence, and the support model (`community-sample`: the customer deploys, owns and operates it) |
| `capabilities.yaml` | What LMA does: every capture method and Virtual Participant platform, both speech engines, the meeting assistant and its tools, skills and MCP servers, the voice assistant, meeting preparation, live guidance, notifications, whiteboards, summaries and summary profiles, video and key frames, downloads, the export, events, consent, retention, presets, hosting models, access control, the hosted MCP server's two endpoints and every tool, the CLI and the SDK |
| `extension-points.yaml` | Every documented way to change LMA's behaviour, classed by the contract's gap taxonomy (configuration, hook, extension, core enhancement, fork, other), with effort and upgrade risk |
| `limitations.yaml` | Where LMA stops, with numbers, how much each limit matters, the question that detects it and what mitigates it |
| `discovery-questions.yaml` | Questions that test fit, each written to be asked to a customer as it stands, with what each answer implies |
| `sizing.yaml` | The inputs that size a deployment, and LMA's own cost estimators |
| `deployment.yaml` | How and where LMA is deployed, and its prerequisites |
| `delivery.yaml` | What a delivery does first, a walking skeleton, and typical work items |
| `changes.yaml` | What each of the last ten releases changed, and what an upgrade requires someone to act on |
| `skills/lma-solution-fit/SKILL.md` | An Agent Skill that tells an assistant how to read the profile and assess fit |

Every capability, extension point and limitation names the files in this repository
that show it (`evidence`), and the checks below fail when one of them no longer
exists. Items are cited as `<kind>:<id>`, for example
`limitation:nova-sonic-concurrent-streams`, and an id keeps its meaning once
published.

## Getting the profile for a release

Each GitHub Release, from the first release that includes the profile, has three
assets:

| Asset | What it is |
|---|---|
| `solution-profile-live-meeting-assistant-<version>.tar.gz` | The profile, with one top-level directory `solution-profile/`, its skill and `build.json` |
| `solution-profile-live-meeting-assistant-<version>.tar.gz.sha256` | The tarball's SHA-256, in `sha256sum` format |
| `solution-profile-live-meeting-assistant-<version>.build.json` | A copy of the tarball's `build.json`: `id`, `version`, `tag`, `commit` and `schema_version`, with no timestamps |

To verify a download:

```bash
sha256sum -c solution-profile-live-meeting-assistant-<version>.tar.gz.sha256
tar -xzf solution-profile-live-meeting-assistant-<version>.tar.gz
```

The built manifest carries `source.tag` and `source.commit`; the checked-in one does
not, because a commit cannot name its own hash. The vendored contract
(`solution-profile/schema/`) is not in the tarball.

The tarball is deterministic: the same commit and tag always build the same tar
stream, and with the same Python and zlib the same compressed bytes. To build it
yourself from a checkout:

```bash
make solution-profile-build              # into dist/, tag v<version>
make solution-profile-build TAG=v0.4.1
```

## The checks

`make solution-profile-check` runs two halves:

1. **The contract's reference validator**, vendored unchanged, which checks every
   file against the contract's schema, every `<kind>:<id>` reference, that every
   evidence path exists, and that the manifest's `version` equals `VERSION`.
2. **Cross-checks against the code**, which fail, naming the item and where it
   belongs, when the code has something the profile does not cover:
   - every tool the hosted MCP server offers (`MCP_TOOLS` and `MEETING_TOOLS` in
     `lma-ai-stack/source/lambda_functions/mcp_analytics/`) is tagged
     `mcp-tool:<name>` on a capability;
   - every platform the Virtual Participant accepts (`_PLATFORMS` in
     `mcp_analytics/tools/platform_helper.py`) is tagged `vp-platform:<VALUE>`;
   - every shipped configuration preset (`lma_config/presets/<id>.json`) is tagged
     `preset:<id>`;
   - the published deployment option's Regions are the Regions `aws-release.sh`
     publishes to;
   - each of the latest ten releases in `CHANGELOG.md` has a `changes.yaml` entry,
     and one with an item users must act on records it;
   - every `<page>.md#<anchor>` evidence entry names a heading that page has.

A tag that names something the code no longer has fails too.

**Versions.** The contract's version pattern is `X.Y.Z` with an optional
`-suffix`. On `develop`, `VERSION` holds a development version such as
`0.4.1.dev1`, so the check passes the validator its contract form, `0.4.1-dev1`,
and the manifest carries the same form. `make version V=...` updates the manifest
with the rest of the version files.

**Release rules.** When `VERSION` is a final release (`X.Y.Z`), or with
`SOLUTION_PROFILE_RELEASE=1`, the check also requires `reviewed_for_version` to
equal `version` and `changes.yaml` to have an entry for it. So a release cannot be
built until somebody has reviewed the profile for it.

`make test-solution-profile` tests the check itself (it fails when a tool, platform,
preset, Region, release or evidence file is added or removed without the profile
following) and the build (two builds are byte-identical, the tarball's layout and
metadata, and the validator accepts the unpacked tarball). The pull-request checks
run both whenever the profile, the files its cross-checks read, or any file it cites
as evidence changes, and on every push to `develop` and `main`.

## Publishing

A release is cut as before: `aws-release.sh` publishes the templates to the public
buckets and deploys the documentation site, and the GitHub Release is then created
for the tag. Publishing that release runs
`.github/workflows/solution-profile-release.yml`, which:

1. checks the tag is `vX.Y.Z` and equals `VERSION` at that tag;
2. applies the contract's release rules and the cross-checks;
3. builds the tarball, validates the unpacked copy, and
4. uploads the tarball, its `.sha256` and its `.build.json` to the release.

The workflow can also be run by hand from the Actions tab for one tag, to attach the
profile to a release published before the workflow ran or to re-run it. A tag from
before the profile existed has nothing to build and fails at the second step.

## Reviewing the profile for a release

The curated files are written by people, so each release needs a review. It is a
step of the changelog skill (`.claude/skills/prepare-changelog.md`, "Review the
solution profile"):

1. Read the release's CHANGELOG section and update `capabilities.yaml`,
   `extension-points.yaml`, `limitations.yaml` and the other files for what changed.
   Keep ids stable: mark a retired item `status: retired` for at least one release
   rather than deleting it.
2. Add a `changes.yaml` entry for the release, with its `action_required` items.
3. Set `reviewed_for_version` to the release's version.
4. Run `make solution-profile-check` with the release's `VERSION`.

The profile is public. Describe each feature generically, and never name a customer
or an internal programme in it.

## The vendored contract

`solution-profile/schema/v1/` holds the Solution Profile contract, version 1, copied
unchanged: its README (the files, their fields and the gap taxonomy), its JSON
Schema and its reference validator. `solution-profile/schema/README.md` records
where the copy came from, pins each file's SHA-256 (checked by the tests), and lists
the points about the contract that were raised with its owners. The copy is never
edited here; a new version of the contract is copied in whole.
