#!/usr/bin/env python3
"""Run the SRT security assessment.

Exits non-zero in CI when open findings remain; exits 0 locally so iteration
isn't blocked.
"""

import os
import subprocess  # nosec B404 - hardcoded commands only
import sys
from pathlib import Path


def main():
    project_root = Path(__file__).parent.parent.parent
    srt_dir = project_root / ".srt"
    binary = srt_dir / "srt"
    is_ci = bool(os.getenv("CI") or os.getenv("GITLAB_CI") or os.getenv("GITHUB_ACTIONS"))

    if not binary.exists():
        print(f"❌ SRT not found at {binary}. Run 'make srt-setup' first.")
        sys.exit(1)

    cmd = (
        f"./srt assess -y -p {project_root} "
        "--no-diagrams --no-threat-models --no-license-update"
    )
    # nosemgrep: python.lang.security.audit.subprocess-shell-true.subprocess-shell-true
    result = subprocess.run(  # nosec B602 - hardcoded commands
        cmd, shell=True, cwd=srt_dir, capture_output=True, text=True, check=False
    )

    if result.returncode != 0:
        print("❌ SRT scan failed")
        if result.stdout:
            print(result.stdout)
        if result.stderr:
            print(result.stderr, file=sys.stderr)
        sys.exit(1)

    print(result.stdout)

    # SRT prints "Open: N" in its summary; treat any N>0 as findings.
    if "Open: 0" not in result.stdout:
        if is_ci:
            print("\n❌ Security issues found.")
            sys.exit(1)
        print("\n⚠️  Security issues found. Run 'make srt-fix' to triage.")
        sys.exit(0)

    print("\n✅ No open security issues.")


if __name__ == "__main__":
    main()
