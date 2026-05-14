# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
"""Standalone entry-point for ``lma-load``.

Kept deliberately dependency-free so the console-script wrapper can always be
loaded; real imports happen inside :func:`safe_main`.
"""

from __future__ import annotations

import sys


def safe_main() -> None:
    """Wrapper around :func:`lma_load.cli.load_command` with dependency checks."""
    try:
        from lma_load.cli import load_command  # noqa: WPS433
    except ImportError as exc:
        _print_missing_dependency_error(exc)
        raise SystemExit(1) from None
    load_command()


def _print_missing_dependency_error(exc: ImportError) -> None:
    missing = exc.name or "unknown"
    print(
        f"\n[lma-load] Error: missing required dependency — {missing!r}\n"
        f"  {exc}\n"
        "\nInstall with:\n"
        "    pip install -e utilities/load-simulator\n"
        "    # optional extras for specific drivers:\n"
        "    pip install -e 'utilities/load-simulator[websocket]'   # ws driver\n"
        "    pip install -e 'utilities/load-simulator[upload]'      # upload driver\n"
        "    pip install -e 'utilities/load-simulator[vp]'          # vp driver\n"
        "    pip install -e 'utilities/load-simulator[all]'         # everything\n",
        file=sys.stderr,
    )
