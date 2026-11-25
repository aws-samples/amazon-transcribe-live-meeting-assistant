<!--
Copyright (c) 2025 Amazon.com
This file is licensed under the MIT-0 License.
See the LICENSE file in the project root for full license information.
-->

# Security test results

One folder per release, each holding a counts-only snapshot of that release's
security tests: SRT, the static role checks, the live access tests and the
OWASP ZAP scan.

```
test-results/
└── <version>/
    ├── summary.md     # to read
    └── summary.json   # the same content, for tools
```

What a snapshot contains, how to read it and how one is produced are described
in [Security Testing: Release snapshots](../../docs/security-testing.md#release-snapshots).
Snapshots are written by `scripts/security/curate_snapshot.py`, never by hand,
and every committed snapshot is checked by `scripts/security/tests/test_curate_snapshot.py`.
