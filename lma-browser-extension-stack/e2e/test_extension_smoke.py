# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""The built browser extension, loaded into headless Chromium. No AWS, no network.

What the Jest tests in ``src/`` cannot show: that the production build loads as a
Manifest V3 extension (manifest, service worker, side panel), that the extension id
is the one the Cognito app client accepts as its sign-in callback, that the panel
renders its sign-in screen and, once signed in, its meeting details, that the Login
button starts Cognito's hosted sign-in with that callback and exchanges the code it
returns, and that the recorder is injected into pages.

Every request to the network is answered here: the stand-in Cognito token endpoint
is fulfilled with unsigned tokens, the fixture page is served from 127.0.0.1, and
anything else is aborted and fails the test that made it.

Needs the build (``npm run build`` in ``lma-browser-extension-stack``) and
Playwright's Chromium; ``make test-browser-extension-e2e`` installs it and runs this.
The live counterpart, against a deployed stack, is
``integ-tests/test_browser_extension.py``.
"""

from __future__ import annotations

import functools
import http.server
import json
import re
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import extension_browser as eb
import pytest
from playwright import sync_api

OFFLINE = eb.OFFLINE_CONFIG
TOKEN_ENDPOINT = f"{OFFLINE['cognitoDomain']}/oauth2/token"
SIGNED_IN_NAME = "E2E Test User"


def _tokens() -> dict[str, str]:
    exp = int(time.time()) + 3600
    return {
        "access_token": eb.unsigned_jwt({"sub": "e2e", "exp": exp, "token_use": "access"}),
        "id_token": eb.unsigned_jwt(
            {"sub": "e2e", "exp": exp, "name": SIGNED_IN_NAME, "email": "e2e@example.invalid"}
        ),
        "refresh_token": "e2e-refresh-token",
        "token_type": "Bearer",
        "expires_in": 3600,
    }


class _Network:
    """Answers the browser's requests to the network and records them."""

    def __init__(self, local_origin: str) -> None:
        self.local_origin = local_origin
        self.token_requests: list[dict[str, list[str]]] = []
        self.refused: list[str] = []

    def handle(self, route) -> None:
        request = route.request
        url = request.url
        if url.startswith(self.local_origin):
            route.continue_()
        elif url == TOKEN_ENDPOINT and request.method == "POST":
            self.token_requests.append(parse_qs(request.post_data or ""))
            route.fulfill(
                status=200,
                content_type="application/json",
                headers={"Access-Control-Allow-Origin": "*"},
                body=json.dumps(_tokens()),
            )
        else:
            self.refused.append(url)
            route.abort()


@pytest.fixture(scope="module")
def fixture_server() -> Iterator[str]:
    handler = functools.partial(_QuietHandler, directory=str(eb.FIXTURES_DIR))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A002 - the base class's signature
        pass


@pytest.fixture(scope="module")
def browser(tmp_path_factory, fixture_server):
    """One Chromium with the staged extension, shared by the module's tests."""
    root = tmp_path_factory.mktemp("extension-e2e")
    extension = eb.stage_extension(root / "extension", config=OFFLINE)
    network = _Network(fixture_server)
    with sync_api.sync_playwright() as playwright:
        context, extension_id = eb.launch(playwright, extension, root / "profile")
        context.route(re.compile(r"^(https?|wss?)://"), network.handle)
        try:
            yield {
                "context": context,
                "extension_id": extension_id,
                "network": network,
                "staged": extension,
            }
        finally:
            context.close()


@pytest.fixture
def network(browser) -> Iterator[_Network]:
    net = browser["network"]
    net.token_requests.clear()
    net.refused.clear()
    yield net
    assert not net.refused, f"the extension tried to reach the network: {net.refused}"


@pytest.fixture
def signed_out(browser) -> None:
    eb.service_worker(browser["context"]).evaluate("chrome.storage.local.clear()")


def _open_panel(browser):
    page = browser["context"].new_page()
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(eb.panel_url(browser["extension_id"]))
    return page, errors


def test_extension_loads_with_the_id_the_sign_in_callback_names(browser) -> None:
    assert browser["extension_id"] == eb.expected_extension_id(), (
        "the loaded extension's id is not the one lma-cognito-stack registers as a "
        "callback (https://<id>.chromiumapp.org/), so sign-in would be refused; the id "
        "comes from the `key` in public/manifest.json"
    )


def test_manifest_is_the_staged_release_version(browser) -> None:
    worker = eb.service_worker(browser["context"])
    manifest = worker.evaluate("chrome.runtime.getManifest()")
    assert manifest["manifest_version"] == 3
    assert manifest["version"] == eb.chrome_version(eb.repo_version())
    assert manifest["side_panel"]["default_path"] == eb.PANEL_PAGE


def test_toolbar_button_opens_the_side_panel(browser) -> None:
    # Set by public/service-worker.js when it starts: without it, the toolbar
    # button does nothing.
    worker = eb.service_worker(browser["context"])
    behavior = worker.evaluate("chrome.sidePanel.getPanelBehavior()")
    assert behavior == {"openPanelOnActionClick": True}


def test_side_panel_shows_the_sign_in_screen(browser, network, signed_out) -> None:
    page, errors = _open_panel(browser)
    try:
        login = page.get_by_role("button", name="Login")
        sync_api.expect(login).to_be_visible(timeout=15_000)
        sync_api.expect(page.locator("h2.header")).to_contain_text("Meeting Assistant")
        sync_api.expect(page.locator(".version")).to_have_text(eb.chrome_version(eb.repo_version()))
        sync_api.expect(page.get_by_role("button", name="Start Listening")).to_have_count(0)
        assert not errors, errors
    finally:
        page.close()


def test_login_starts_cognito_sign_in_and_exchanges_the_code(browser, network, signed_out) -> None:
    """The Login button, with the hosted sign-in window stood in for.

    ``chrome.identity.launchWebAuthFlow`` opens a browser window Playwright cannot
    drive, so the page's copy of it is replaced by one that records the request and
    answers with the redirect Cognito would send. What the panel does with the code
    is real: it posts it to the token endpoint, which is answered here.
    """
    page, errors = _open_panel(browser)
    try:
        sync_api.expect(page.get_by_role("button", name="Login")).to_be_visible(timeout=15_000)
        page.evaluate(
            """(redirect) => {
              window.__lmaAuthFlows = [];
              chrome.identity.launchWebAuthFlow = async (details) => {
                window.__lmaAuthFlows.push(details);
                return redirect + '?code=e2e-authorization-code';
              };
            }""",
            f"https://{browser['extension_id']}.chromiumapp.org/",
        )
        page.get_by_role("button", name="Login").click()
        sync_api.expect(page.get_by_role("button", name="Start Listening")).to_be_visible(
            timeout=15_000
        )

        flows = page.evaluate("window.__lmaAuthFlows")
        assert len(flows) == 1 and flows[0]["interactive"] is True, flows
        url = urlsplit(flows[0]["url"])
        assert f"{url.scheme}://{url.netloc}" == OFFLINE["cognitoDomain"]
        assert url.path == "/login"
        query = parse_qs(url.query)
        callback = f"https://{eb.expected_extension_id()}.chromiumapp.org/"
        assert query["response_type"] == ["code"]
        assert query["client_id"] == [OFFLINE["clientId"]]
        assert query["redirect_uri"] == [callback]
        assert query["scope"] == ["email openid profile"]

        assert network.token_requests == [
            {
                "grant_type": ["authorization_code"],
                "code": ["e2e-authorization-code"],
                "client_id": [OFFLINE["clientId"]],
                "redirect_uri": [callback],
            }
        ]
        stored = eb.service_worker(browser["context"]).evaluate(
            "chrome.storage.local.get('authTokens')"
        )
        assert stored["authTokens"]["refresh_token"] == "e2e-refresh-token"
        assert not errors, errors
    finally:
        page.close()


def test_signed_in_panel_shows_the_meeting_details(browser, network) -> None:
    eb.service_worker(browser["context"]).evaluate(
        "(tokens) => chrome.storage.local.set({authTokens: tokens})", _tokens()
    )
    page, errors = _open_panel(browser)
    try:
        sync_api.expect(page.get_by_role("button", name="Start Listening")).to_be_visible(
            timeout=15_000
        )
        # The name is taken from the signed-in user's id token.
        sync_api.expect(page.get_by_placeholder("Your name")).to_have_value(SIGNED_IN_NAME)
        sync_api.expect(page.get_by_placeholder("Meeting room topic")).to_have_value("")
        sync_api.expect(page.get_by_text("Platform Detected:")).to_be_visible()
        sync_api.expect(page.get_by_role("button", name="Log out")).to_be_visible()

        # Start Listening with no topic asks for one and does not start.
        page.get_by_role("button", name="Start Listening").click()
        sync_api.expect(page.get_by_text("Topic required.")).to_be_visible()
        sync_api.expect(page.get_by_role("button", name="Agree")).to_be_hidden()
        assert not errors, errors
    finally:
        page.close()


def test_recorder_is_injected_into_a_meeting_page(browser, network, fixture_server) -> None:
    page = browser["context"].new_page()
    try:
        page.goto(f"{fixture_server}/meeting.html")
        frame = page.locator("iframe#permissionsIFrame")
        sync_api.expect(frame).to_have_count(1, timeout=15_000)
        src = frame.get_attribute("src") or ""
        assert src == (
            f"chrome-extension://{browser['extension_id']}/content_scripts/recorder/recorder.html"
        )
        assert "display-capture" in (frame.get_attribute("allow") or "")
    finally:
        page.close()


def test_staged_extension_holds_what_the_manifest_names(browser) -> None:
    staged: Path = browser["staged"]
    manifest = json.loads((staged / "manifest.json").read_text())
    named = [manifest["background"]["service_worker"], manifest["side_panel"]["default_path"]]
    for script in manifest["content_scripts"]:
        named += script["js"]
    for entry in manifest["web_accessible_resources"]:
        named += entry["resources"]
    missing = [path for path in named if not (staged / path).is_file()]
    assert not missing, f"the manifest names files the build does not contain: {missing}"
