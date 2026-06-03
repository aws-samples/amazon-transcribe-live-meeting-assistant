#!/usr/bin/env python3
"""Open the SRT dashboard for interactive triage.

SRT v1.0.x has no ``srt fix`` subcommand; triage is done in
``.srt/dashboard.html`` (or by editing ``.srt/issues.json`` directly).
"""

import os
import platform
import subprocess  # nosec B404 - hardcoded commands only
import sys
import webbrowser
from pathlib import Path


def main():
    project_root = Path(__file__).parent.parent.parent
    srt_dir = project_root / ".srt"
    binary = srt_dir / "srt"
    dashboard = srt_dir / "dashboard.html"
    issues_file = srt_dir / "issues.json"

    if not binary.exists():
        print("❌ SRT not found. Run 'make srt-setup' first.")
        sys.exit(1)
    if not dashboard.exists() or not issues_file.exists():
        print("❌ No SRT scan results. Run 'make srt-scan' first.")
        sys.exit(1)

    print(f"Dashboard: {dashboard}")
    print(f"Issues DB: {issues_file}")
    print(
        "\nIn the dashboard you can suppress, resolve, or reopen findings.\n"
        "After triaging, commit .srt/issues.json to share decisions with the team:\n"
        '  git add .srt/issues.json\n'
        '  git commit -m "chore(security): triage SRT findings"\n'
    )

    # Try to open the dashboard. On headless systems, fall back to printing
    # a CLI summary plus the file path.
    opened = False
    try:
        if platform.system() != "Linux" or os.environ.get("DISPLAY"):
            opened = webbrowser.open(f"file://{dashboard}")
    except Exception:  # noqa: BLE001
        pass

    if not opened:
        print("CLI summary (`srt status -a`):\n")
        try:
            subprocess.run(  # nosec B603 - calling our own binary
                [str(binary), "status", "-p", str(project_root), "-a"],
                check=False, timeout=60,
            )
        except Exception:  # noqa: BLE001
            pass
        print(f"\n💡 Open in a browser: file://{dashboard}")


if __name__ == "__main__":
    main()
