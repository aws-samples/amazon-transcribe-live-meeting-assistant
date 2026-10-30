# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""The VP image's two CloakBrowser wrappers stay on one version.

The Dockerfile pip-installs `cloakbrowser` only to download the patched
Chromium at build time; at runtime the npm `cloakbrowser` client (package.json)
launches that binary. Each wrapper version pins its own Chromium build, so
keeping the two versions equal keeps the binary the pip wrapper installs the one
the npm client expects. Dependabot bumps only the npm side, so a bump fails here
until the Dockerfile ARG is moved with it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"


def _dockerfile_pip_version() -> str:
    text = (BACKEND / "Dockerfile").read_text(encoding="utf-8")
    match = re.search(r"^ARG CLOAKBROWSER_PIP_VERSION=(\S+)\s*$", text, re.MULTILINE)
    assert match, "Dockerfile no longer declares ARG CLOAKBROWSER_PIP_VERSION"
    return match.group(1)


def _npm_version() -> str:
    pkg = json.loads((BACKEND / "package.json").read_text(encoding="utf-8"))
    return pkg["dependencies"]["cloakbrowser"]


def test_npm_cloakbrowser_is_an_exact_pin():
    assert re.fullmatch(r"\d+\.\d+\.\d+", _npm_version()), (
        "package.json must pin cloakbrowser exactly so it can be matched in the Dockerfile"
    )


def test_dockerfile_pip_pin_matches_npm_cloakbrowser():
    assert _dockerfile_pip_version() == _npm_version(), (
        "CLOAKBROWSER_PIP_VERSION in backend/Dockerfile must equal the cloakbrowser "
        "version in backend/package.json"
    )
