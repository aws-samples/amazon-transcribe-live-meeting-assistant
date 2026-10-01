---
title: "Test Coverage"
---

<!--
Copyright (c) 2025 Amazon.com
This file is licensed under the MIT License.
See the LICENSE file in the project root for full license information.
-->

# Test Coverage

```bash
make test-coverage            # every suite, Python and Node (slow)
make test-coverage-python     # the Python suites only (no npm, much faster)
```

Until this existed the repository had no coverage measurement at all, so
"coverage" was a count of test files rather than of lines. `make test-coverage`
runs every suite under a coverage tool and prints one table.

**It reports; it does not enforce.** Each component has a recorded floor and the
table says whether it is met, but a component below its floor still exits 0 — see
[Floors](#floors). The target is not part of the pull-request pipeline either: it
runs every suite and compiles the TypeScript ones first, which would roughly
double the time developers wait for feedback they cannot yet act on.

## Reading the table

```
Component              Lang    Lines  Covered  Total  Files  No data
---------------------  ------  -----  -------  -----  -----  -------
LMA SDK                python  19.8%  564      2854   15     15
LMA CLI                python  14.9%  229      1536   8      8
Lambda functions       python  20.6%  1116     5416   27     37
Lambda layers          python  48.8%  240      492    18     0
ASR MicroVM            python  43.6%  2101     4816   16     16
VP backend             node    37.9%  6160     16270  24     14*
WebSocket transcriber  node    70.1%  2383     3399   11     4*
React UI               node    14.8%  827      5599   180    4*
```

| Column | Meaning |
|---|---|
| `Lines` | Line (statement) coverage. |
| `Covered` / `Total` | Statements executed, and the denominator they are over. |
| `Files` | Source files the suite actually loaded. |
| `No data` | Source files **no test imports at all**. |

That last column is the one to watch, and the reason the numbers here are lower
than a naive run would report.

A coverage tool measures what runs. A module that nothing imports never appears
in its report — so the least-covered code in a component drops out of the
denominator entirely and the percentage flatters the codebase. For the Python
suites every source file on disk is accounted for: unimported files are parsed
for their statement count and credited nothing. `Lambda functions` reads
27 files measured and 37 with no data, which reconciles with the 64 non-test
Python files under `lma-ai-stack/source/lambda_functions/`.

The Node tools (c8 for the two `node --test` suites, vitest's v8 provider for the
UI) report only files that were loaded, and there is no cheap way to count
statements in a TypeScript file without compiling it. For those rows the
unimported files are counted and marked with `*`, and are **excluded** from the
percentage — so the three Node percentages are optimistic by roughly the
proportion of files in that column.

Test files are excluded from every measurement, so a suite cannot raise its own
number by being large.

## What the baseline says

Two rows deserve comment:

`Lambda layers` was at **0.0%** when coverage was first measured — the shared
code every Lambda imports had no suite at all, despite holding
`normalize_transcript_segments` and the transcript TTL handling, which have
misbehaved in production before. It now sits just under half, with the remaining
gap concentrated in four packages that are still untouched:

| Layer package | Lines |
|---|---|
| `eventprocessor_utils` | 75% |
| `graphql_helpers` | 100% |
| `sentiment` | 100% |
| `appsync_utils` | 0% |
| `lambda_utils` | 0% |
| `sns_utils` | 0% |
| `transcript_batch_processor` | 0% |

`transcript_batch_processor` needs `aws_lambda_powertools` installed as a test
dependency before it can be imported at all, which is why it is still at zero.

`WebSocket transcriber` at **70.1%** is the best-covered component and shows what
the others could look like.

## How each component is measured

| Component | Tool | Notes |
|---|---|---|
| LMA SDK, LMA CLI | `pytest-cov` | Straightforward: suite beside an installed package. |
| Lambda functions | `pytest-cov`, accumulated | Each function's tests import their module as a sibling, so a single pytest over the tree collides on duplicate module names — the same reason `make test-lambdas` loops one directory at a time. Coverage accumulates into one data file across those runs. |
| Lambda layers | `pytest-cov`, accumulated | Its suites sit beside the packages they cover and need the layer root importable (`from sentiment import ...`), so pytest runs from that root rather than from each test's own directory. A package with no suite still appears, at zero. |
| ASR MicroVM | `pytest-cov` | Uses the component's own `.venv`; `pytest-cov` is pinned in its `requirements-dev.txt`. |
| VP backend, WebSocket transcriber | `c8` | Wraps the same `node --test` the suite normally runs. Both projects emit source maps, so results land on `src/*.ts` rather than on the compiled output. |
| React UI | `vitest --coverage` | v8 provider. Reports all files, so its `No data` count is small. |

The driver is `scripts/coverage_report.py`; `--only NAME` narrows it to matching
components and `JSON=path` (or `--json path`) also writes the numbers as JSON.

The exit code reflects only whether the suites **ran**. A suite that fails, or a
component whose source roots match nothing on disk, is reported as `n/a` with a
reason and exits non-zero — a misconfigured root would otherwise look like a
component whose every file is covered.

## Floors

`scripts/coverage_floors.json` records a floor per component, and the `Floor`
column reports whether each is met:

```
Component         Lang    Lines  Covered  Total  Files  No data  Floor
LMA SDK           python  19.8%  564      2854   15     15       19%    OK
```

**The floors are advisory.** A component below its floor is reported as `BELOW`
and explained under `Notes:`, and the run still exits 0 — nothing here can fail a
build. `--enforce` makes a breach exit non-zero, and no `make` target passes it;
adding it to one is the single change that turns this from reporting into a gate.

Each floor is the figure measured on 2026-09-22 rounded down to a whole percent,
so there is under a point of headroom. They are set to catch coverage going
*backwards*, not to demand that anyone improve it first. Three things follow:

- A component that drops below its floor has regressed. Find out what stopped
  being covered rather than lowering the number.
- A component that climbs more than two points above its floor gets a note
  suggesting you raise it, so a gain is held rather than banked as slack.
- A component with no entry at all is reported as `—` with a note, not silently
  treated as passing.

`make test-coverage JSON=path` writes the same numbers, each component's floor,
and its verdict as JSON, for anything that wants to track the trend over time.

The sequence is deliberate: measure, record, then enforce. Turning on `--enforce`
is worth doing once the numbers have been watched long enough to be trusted —
a scheduled pipeline is the natural place for that, since nothing there is
waiting on the result.

## See also

- [Developer Guide](developer-guide.md#continuous-integration) — the checks that do gate a pull request
