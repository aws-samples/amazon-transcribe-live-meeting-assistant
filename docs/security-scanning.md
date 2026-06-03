---
title: "Security Scanning (SRT)"
---

# Security Scanning with SRT

LMA integrates the AWS [**Sample Security Review Tool (SRT)**](https://github.com/aws-samples/sample-security-review-tool) for automated security scanning across the codebase, CloudFormation templates, and dependencies. SRT bundles Bandit (Python), Semgrep (SAST), Checkov (IaC), Syft (SBOM), and an AWS-resource security-matrix into a single dashboard with persistent suppression tracking.

> **Note:** SRT is the upstream-renamed successor to **DSR** (Deliverable Security Review). LMA's legacy `.dsr/issues.json` suppressions can be migrated — see [Migrating from DSR](#migrating-from-dsr).

## Quick Start

```bash
make srt           # setup → scan → prompt to open dashboard
make srt-setup     # download and configure the SRT binary
make srt-scan      # run the security assessment
make srt-fix       # open the dashboard for interactive triage
```

After triaging, commit `.srt/issues.json` to share suppression decisions with the team and CI:

```bash
git add .srt/issues.json
git commit -m "chore(security): triage SRT findings"
```

SRT is not part of `make lint` or `make test` — it runs automatically in GitLab CI on merge requests targeting `develop`.

## How It Works

`make srt-setup` downloads the latest SRT binary from [GitHub Releases](https://github.com/aws-samples/sample-security-review-tool/releases) into `.srt/`. `make srt-scan` runs `srt assess`, which:

1. Identifies CloudFormation / SAM templates and Python source files.
2. Runs Bandit, Semgrep, Checkov, Syft, and the AWS security-matrix in parallel.
3. Produces an interactive dashboard at `.srt/dashboard.html`.
4. Persists findings in `.srt/issues.json` with status (`Open` / `suppressed` / `resolved` / `reopened`).

On a fresh checkout without any suppressions, SRT will report a number of findings across Bandit (Python), Semgrep (SAST), Checkov (IaC), and the AWS security-matrix. After running `make srt-migrate-dsr` to import the team's existing suppression decisions, the open count should drop to zero or near-zero.

## Make Targets

| Target | Description |
|--------|-------------|
| `make srt` | Full workflow: setup → scan |
| `make srt-setup` | Download and configure SRT (idempotent; pin version with `SRT_VERSION=v1.0.2`) |
| `make srt-scan` | Run the assessment. Non-zero exit in CI on open findings; zero locally |
| `make srt-fix` | Open `.srt/dashboard.html` for triage; falls back to `srt status -a` on headless servers |
| `make srt-migrate-dsr` | One-shot: migrate suppressions from `.dsr/issues.json` → `.srt/issues.json` |

## Suppression Workflow

SRT tracks each finding's status in `.srt/issues.json`:

| Status | Meaning |
|--------|---------|
| `Open` | New finding — needs review |
| `suppressed` | False positive or accepted risk — always include a `suppressionReason` |
| `resolved` | Fixed in code — SRT will reopen if the issue resurfaces |
| `reopened` | A previously-resolved issue has returned |

`.srt/issues.json` is tracked in git via a negative-gitignore rule (same pattern as `.dsr/issues.json`). The binary, `.venv`, scan output, and config files are all gitignored.

**Triage workflow:**

1. `make srt-scan`
2. `make srt-fix` — opens the dashboard
3. Filter to HIGH priority + Open
4. Suppress false positives with a written rationale; fix genuine issues in code
5. `make srt-scan` again to confirm `Open: 0`
6. `git add .srt/issues.json && git commit`

You can also edit `.srt/issues.json` directly — set `status` to `suppressed` or `resolved` and add a `suppressionReason` field.

## CI/CD Integration

`.gitlab-ci.yml` defines a `security_review` stage with a single `srt_security_review` job that runs only on merge requests targeting `develop`:

```yaml
srt_security_review:
  stage: security_review
  rules:
    - if: $CI_MERGE_REQUEST_TARGET_BRANCH_NAME == "develop"
      when: on_success
  script:
    - make srt-setup
    - make srt-scan
```

The job uses `tags: [size:large]` (4 vCPU / 16 GB RAM) — SRT's scanner toolchain needs more memory than a default runner. Adjust the tag to match your runner pool.

**Artifacts captured on every run (1 week retention):**

| File | Contents |
|------|----------|
| `srt-issues.json` | Full issue database |
| `srt-dashboard.html` | Interactive HTML dashboard |
| `srt-bandit-summary.json` | Python static-analysis summary |
| `srt-semgrep-summary.json` | SAST findings summary |
| `srt-syft-summary.json` | SBOM summary |
| `srt-project-summary.md` | Project summary |

## Migrating from DSR

Run the one-shot migration to port existing suppressions:

```bash
make srt-migrate-dsr
```

This reads `.dsr/issues.json`, drops `Open` entries (SRT will rediscover them), normalizes `suppression_reason` → `suppressionReason`, and writes `.srt/issues.json`. The `.dsr/` directory is not modified.

After migrating, run `make srt-scan` to verify suppressions match the new findings. If a suppression doesn't match (e.g., source moved by a few lines), SRT will report it as `Open` again — re-suppress via the dashboard.

## Pinning the SRT Version

```bash
SRT_VERSION=v1.0.2 make srt-setup
```

Set `SRT_VERSION` as a GitLab CI/CD project variable for reproducible pipeline scans.

## Files & Artifacts

```
.srt/
├── srt                              # Binary (gitignored, ~118 MB)
├── srt-cli-v*.tar.gz                # Downloaded archive (gitignored)
├── .venv/                           # Scanner toolchain (gitignored)
├── srtconfig.json                   # AWS profile + region (gitignored, regenerated per machine)
├── settings.json                    # Project ID + last-scan date (gitignored)
├── issues.json                      # Suppression database ← committed
├── dashboard.html                   # Interactive dashboard (gitignored)
├── *-summary.json                   # Per-scanner summaries (gitignored)
└── <stack-name>/                    # Per-stack analysis dirs (gitignored)
```

## Troubleshooting

**`SRT not found`** — run `make srt-setup`.

**Config prompts hang in CI** — `setup.py` auto-detects CI via `CI`, `GITLAB_CI`, or `GITHUB_ACTIONS`. If none are set, export `CI=1` before running `make srt-setup`.

**Platform not supported** — SRT supports Linux x64/arm64 and macOS x64/arm64. Windows is not yet available.

**Reconfigure SRT** — `cd .srt && ./srt config`

## Learn More

- [SRT GitHub Repository](https://github.com/aws-samples/sample-security-review-tool)
- [Well-Architected Assessment](./well-architected.md)
- [Infrastructure & Security](./infrastructure-and-security.md)
