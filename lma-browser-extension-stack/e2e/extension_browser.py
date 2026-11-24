# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Load the built browser extension into Playwright's Chromium.

Shared by the per-pull-request smoke test (``test_extension_smoke.py`` beside this
file, no network) and the live test against a deployed stack
(``integ-tests/test_browser_extension.py``).

The extension is staged the way the stack's CodeBuild project packages it (see the
BuildSpec in ``lma-browser-extension-stack/template.yaml``): the ``npm run build``
output, with ``<CHROME_VERSION_TOKEN>`` and ``<VERSION_TOKEN>`` in ``manifest.json``
replaced and ``lma_config.json`` written. Chromium then loads the staged copy unpacked, through
``launch_persistent_context`` with ``--load-extension``; extensions need a persistent
profile.

Headless needs Playwright's full Chromium build (``channel="chromium"``, installed
by ``playwright install --no-shell chromium``). The separate headless shell does not
load extensions. ``channel="chromium"`` with ``headless=True`` runs Chromium's new
headless mode, so no X server is needed.
"""

from __future__ import annotations

import json
import re
import shutil
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

E2E_DIR = Path(__file__).resolve().parent
EXTENSION_DIR = E2E_DIR.parent
REPO_ROOT = EXTENSION_DIR.parent
BUILD_DIR = EXTENSION_DIR / "build"
FIXTURES_DIR = E2E_DIR / "fixtures"
COGNITO_TEMPLATE = REPO_ROOT / "lma-cognito-stack" / "deployment" / "lma-cognito-stack.yaml"

#: The page the side panel opens (``side_panel.default_path`` in the manifest).
PANEL_PAGE = "index.html"

#: What the stack's CodeBuild project writes to ``lma_config.json``, with values that
#: resolve nowhere (RFC 2606), for runs that must not reach a stack.
OFFLINE_CONFIG: dict[str, str] = {
    "wssEndpoint": "wss://transcriber.example.invalid/api/v1/ws",
    "clientId": "e2e-test-client",
    "cognitoDomain": "https://auth.example.invalid",
    "cloudfrontEndpoint": "https://app.example.invalid/",
    "recordingDisclaimer": "E2E test disclaimer.",
    "recordingMessage": "E2E test recording message.",
    "stopRecordingMessage": "E2E test stop message.",
}


def chrome_version(version: str) -> str:
    """The release part of an LMA version, in the form a Chrome manifest accepts.

    Chrome accepts one to four dot-separated integers, so a pre-release ``VERSION``
    such as ``0.4.1.dev1`` is staged as ``0.4.1``.
    """
    match = re.match(r"\d+(?:\.\d+){0,3}", version.strip())
    if not match:
        raise ValueError(f"no numeric version in {version!r}")
    return match.group(0)


def repo_version() -> str:
    return (REPO_ROOT / "VERSION").read_text().strip()


def expected_extension_id(template: Path = COGNITO_TEMPLATE) -> str:
    """The extension id the Cognito app client accepts as a sign-in callback.

    The extension signs in through ``chrome.identity.launchWebAuthFlow``, whose
    redirect URI is ``https://<extension id>.chromiumapp.org/``. The id comes from the
    ``key`` in ``manifest.json``, so this is what holds the two together.
    """
    ids = re.findall(r"https://([a-p]{32})\.chromiumapp\.org/", template.read_text())
    if len(set(ids)) != 1:
        raise AssertionError(f"expected one chromiumapp.org callback in {template}, got {ids}")
    return ids[0]


def stage_extension(
    dest: Path,
    *,
    build_dir: Path = BUILD_DIR,
    version: str | None = None,
    config: Mapping[str, Any] | None = None,
) -> Path:
    """Copy a built extension to ``dest`` and finish it as the stack's build does.

    ``config=None`` keeps the ``lma_config.json`` already in ``build_dir`` (an
    extension downloaded from a stack already has its own).
    """
    manifest = build_dir / "manifest.json"
    if not manifest.is_file():
        raise FileNotFoundError(
            f"{manifest} not found: build the extension first "
            "(cd lma-browser-extension-stack && npm install && npm run build)"
        )
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(build_dir, dest)
    staged = dest / "manifest.json"
    text = staged.read_text()
    full = version or repo_version()
    # As the stack's build does: ``version`` takes a form Chrome accepts and
    # ``version_name`` the full version, which the extension's screens show.
    finished = text.replace("<CHROME_VERSION_TOKEN>", chrome_version(full)).replace(
        "<VERSION_TOKEN>", full
    )
    if finished != text:
        staged.write_text(finished)
    if config is not None:
        (dest / "lma_config.json").write_text(json.dumps(dict(config), indent=2))
    return dest


#: Chromium switches for every run: no first-run UI, and pages in background tabs
#: keep their timers, because the live test drives the panel from a tab that is
#: not in front (see integ-tests/browser_extension_probe.py).
BASE_ARGS = (
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-background-timer-throttling",
    "--disable-renderer-backgrounding",
    "--disable-backgrounding-occluded-windows",
)


def launch(
    playwright: Any,
    extension_dir: Path,
    profile_dir: Path,
    *,
    extra_args: Iterable[str] = (),
    headless: bool = True,
) -> tuple[Any, str]:
    """Start Chromium with the extension loaded. Returns (context, extension id)."""
    context = playwright.chromium.launch_persistent_context(
        str(profile_dir),
        channel="chromium",
        headless=headless,
        args=[
            f"--disable-extensions-except={extension_dir}",
            f"--load-extension={extension_dir}",
            *BASE_ARGS,
            *extra_args,
        ],
    )
    worker = service_worker(context)
    return context, worker.url.split("/")[2]


def service_worker(context: Any, timeout_ms: int = 20_000) -> Any:
    """The extension's service worker. It not starting means the extension did not load."""
    for worker in context.service_workers:
        if worker.url.startswith("chrome-extension://"):
            return worker
    return context.wait_for_event(
        "serviceworker",
        predicate=lambda w: w.url.startswith("chrome-extension://"),
        timeout=timeout_ms,
    )


def panel_url(extension_id: str) -> str:
    return f"chrome-extension://{extension_id}/{PANEL_PAGE}"


def unsigned_jwt(claims: Mapping[str, Any]) -> str:
    """A JWT-shaped string the panel can decode. Unsigned: nothing here verifies it.

    The panel's expiry check decodes the payload with a plain ``atob``, which rejects
    the base64url characters ``-`` and ``_``, so a padding claim is grown until the
    encoded payload has neither.
    """
    import base64

    def part(obj: Mapping[str, Any]) -> str:
        raw = json.dumps(dict(obj), separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    payload = part(claims)
    pad = ""
    while "-" in payload or "_" in payload:
        pad += "x"
        payload = part({**claims, "pad": pad})
    return f"{part({'alg': 'none', 'typ': 'JWT'})}.{payload}.e2e"
