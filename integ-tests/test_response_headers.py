# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""The response headers a deployed stack sends, read from its endpoints.

Read-only and credential-free: GET requests to the web UI's CloudFront
distribution, and one POST without a key to the MCP API key endpoint, which
API Gateway refuses with a gateway response. The headers the MCP function sets
on an accepted call are checked by test_mcp_api_key_logging.py, which holds a
key. lma-ai-stack/test/test_web_ui_response_headers.py pins the same headers in
the template; this file checks that the distribution and the API serve them.

A source missing from the Content-Security-Policy fails silently in the
browser, so the origins the app cannot work without (the WebSocket
transcriber, AppSync, Cognito) are checked against this stack's own endpoints.
Whether the app works under the policy is shown by the suites that drive the
UI in Chromium (test_stream_page_audio.py, test_stream_page_video.py,
test_browser_video.py, test_browser_extension.py).
"""

from __future__ import annotations

import fnmatch
import urllib.error
import urllib.parse
import urllib.request

import pytest

TIMEOUT = 30

SECURITY_HEADERS = {
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "X-Content-Type-Options": "nosniff",
    "Cross-Origin-Resource-Policy": "same-origin",
}


def _get(url: str):
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:  # nosec B310 - https stack endpoint
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as err:
        return err.code, err.headers, err.read()


def _csp(headers) -> dict[str, list[str]]:
    value = headers.get("Content-Security-Policy")
    assert value, "the web UI sends no Content-Security-Policy"
    directives = {}
    for part in value.split(";"):
        if part.strip():
            name, *sources = part.split()
            directives[name] = sources
    return directives


def _allows(sources: list[str], url: str) -> bool:
    """Whether a CSP source list admits ``url`` (scheme and host; enough for these sources)."""
    parsed = urllib.parse.urlsplit(url)
    for source in sources:
        if "://" not in source:
            continue
        scheme, host = source.split("://", 1)
        host = host.split("/", 1)[0]
        if scheme == parsed.scheme and fnmatch.fnmatchcase(parsed.hostname or "", host):
            return True
    return False


@pytest.fixture(scope="module")
def web_url(outputs) -> str:
    url = outputs.get("ApplicationCloudfrontEndpoint") or outputs.get("CloudfrontEndpoint")
    if not url:
        pytest.fail("stack has no ApplicationCloudfrontEndpoint output")
    return url.rstrip("/") + "/"


@pytest.fixture(scope="module")
def web(web_url):
    status, headers, _ = _get(web_url)
    assert status == 200, f"GET {web_url} returned HTTP {status}"
    return headers


@pytest.fixture(scope="module")
def csp(web) -> dict[str, list[str]]:
    return _csp(web)


@pytest.fixture(scope="module")
def ui_env(outputs) -> dict[str, str]:
    """The UI build's settings, from the LocalUITestingEnv output (KEY=value pairs)."""
    env = dict(part.split("=", 1) for part in outputs.get("LocalUITestingEnv", "").split() if "=" in part)
    if "VITE_APPSYNC_GRAPHQL_URL" not in env or "VITE_AWS_REGION" not in env:
        pytest.fail("stack's LocalUITestingEnv output names no VITE_APPSYNC_GRAPHQL_URL / VITE_AWS_REGION")
    return env


def test_web_ui_scripts_and_styles(csp) -> None:
    assert csp["script-src"] == ["'self'"]
    assert csp["style-src"] == ["'self'", "'unsafe-inline'"]
    assert csp["default-src"] == ["'self'"]
    assert csp["object-src"] == ["'none'"]
    assert csp["frame-ancestors"] == ["'self'"]


def test_web_ui_policy_has_no_whole_scheme_or_any_host_source(csp) -> None:
    for directive, sources in csp.items():
        for source in sources:
            assert source not in {"*", "https:", "http:", "wss:", "ws:"}, f"{directive} allows {source}"


def test_connect_src_admits_the_websocket_transcriber(csp, outputs) -> None:
    endpoint = outputs.get("LMAWebsocketEndpoint")
    if not endpoint:
        pytest.fail("stack has no LMAWebsocketEndpoint output")
    assert _allows(csp["connect-src"], endpoint), (
        f"connect-src does not admit the transcriber {endpoint}: Stream Audio/Video could not connect"
    )


def test_connect_src_admits_appsync(csp, ui_env) -> None:
    graphql = ui_env["VITE_APPSYNC_GRAPHQL_URL"]
    realtime = graphql.replace("https://", "wss://", 1).replace(".appsync-api.", ".appsync-realtime-api.", 1)
    assert _allows(csp["connect-src"], graphql), f"connect-src does not admit {graphql}"
    assert _allows(csp["connect-src"], realtime), f"connect-src does not admit {realtime}"


@pytest.mark.parametrize("service", ["cognito-idp", "cognito-identity", "ssm", "states", "translate"])
def test_connect_src_admits_the_regional_services_the_ui_calls(csp, ui_env, service) -> None:
    url = f"https://{service}.{ui_env['VITE_AWS_REGION']}.amazonaws.com/"
    assert _allows(csp["connect-src"], url), f"connect-src does not admit {url}"


def test_connect_src_refuses_other_hosts(csp, ui_env) -> None:
    region = ui_env["VITE_AWS_REGION"]
    other = "eu-west-1" if region != "eu-west-1" else "us-east-1"
    for url in ("https://example.com/", f"https://ssm.{other}.amazonaws.com/"):
        assert not _allows(csp["connect-src"], url), f"connect-src admits {url}"


def test_web_ui_security_headers(web) -> None:
    assert web.get("Strict-Transport-Security") == SECURITY_HEADERS["Strict-Transport-Security"]
    assert web.get("X-Content-Type-Options") == "nosniff"
    assert web.get("Cross-Origin-Resource-Policy") == "same-origin"
    assert web.get("Referrer-Policy") == "strict-origin-when-cross-origin"
    assert web.get("X-Frame-Options") == "DENY"


def test_web_ui_permissions_policy(web) -> None:
    value = web.get("Permissions-Policy")
    assert value, "the web UI sends no Permissions-Policy"
    features = dict(part.strip().split("=", 1) for part in value.split(","))
    for feature in ("microphone", "camera", "display-capture"):
        assert features.get(feature) == "(self)", f"{feature} is {features.get(feature)!r}, Stream Audio/Video needs (self)"
    assert features.get("geolocation") == "()"


def test_web_ui_sends_no_opener_policy(web) -> None:
    # The MCP server OAuth popup and an embed opened as a popup post their
    # result to window.opener, which this header would remove.
    assert web.get("Cross-Origin-Opener-Policy") is None


def test_web_ui_server_header_is_cloudfront(web) -> None:
    assert web.get("Server") == "CloudFront"


def test_chat_frame_is_served_with_the_policy(web_url, web) -> None:
    status, headers, body = _get(web_url + "strands-chat.html")
    assert status == 200
    assert headers.get("Content-Security-Policy") == web.get("Content-Security-Policy")
    assert b'<script src="js/strands-chat.js"></script>' in body
    status, headers, _ = _get(web_url + "js/strands-chat.js")
    assert status == 200 and "javascript" in (headers.get("Content-Type") or "")


def test_mcp_gateway_refusal_carries_security_headers(outputs, request) -> None:
    endpoint = outputs.get("MCPServerApiKeyEndpoint")
    if not endpoint:
        request.node.add_marker(pytest.mark.optional_feature)
        pytest.skip("stack has no MCP API key endpoint (EnableMCP=false)")
    post = urllib.request.Request(
        endpoint,
        data=b'{"jsonrpc": "2.0", "id": 1, "method": "ping"}',
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(post, timeout=TIMEOUT) as response:  # nosec B310 - https stack endpoint
            status, headers = response.status, response.headers
    except urllib.error.HTTPError as err:
        status, headers = err.code, err.headers
    assert status in (401, 403), f"a request without a key returned HTTP {status}"
    assert {name: headers.get(name) for name in SECURITY_HEADERS} == SECURITY_HEADERS
