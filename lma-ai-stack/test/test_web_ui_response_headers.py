# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""The response headers the web UI and the MCP API key endpoint send.

The web UI's headers come from the CloudFront response headers policy
(SecurityHeadersPolicy); the MCP endpoint's from its Lambda function
(RESPONSE_HEADERS in mcp_analytics/index.py) and its gateway responses.

A source missing from the Content-Security-Policy fails silently in the
browser, so the directives are pinned exactly here and the origins the app
cannot work without are asserted on their own, with the reason in each message.
integ-tests/test_response_headers.py checks the same headers on a deployed stack.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
from ai_stack_templates import TEMPLATE, flattened, load

ROOT = Path(__file__).resolve().parents[2]
MAIN_TEMPLATE = ROOT / "lma-main.yaml"


UI = ROOT / "lma-ai-stack" / "source" / "ui"
MCP_INDEX = ROOT / "lma-ai-stack" / "source" / "lambda_functions" / "mcp_analytics" / "index.py"

S3 = "https://*.s3.${AWS::Region}.${AWS::URLSuffix} https://s3.${AWS::Region}.${AWS::URLSuffix}"
EXPECTED_CSP = {
    "default-src": "'self'",
    "script-src": "'self'",
    "style-src": "'self' 'unsafe-inline'",
    "img-src": f"'self' data: blob: {S3}",
    "media-src": f"'self' blob: {S3}",
    "font-src": "'self' data:",
    "worker-src": "'self' blob:",
    "frame-src": "'self'",
    "connect-src": " ".join(
        [
            "'self'",
            "https://${AppSyncApiEncrypted.GraphQLDns}",
            "wss://${AppSyncApiEncrypted.RealtimeDns}",
            "https://cognito-idp.${AWS::Region}.${AWS::URLSuffix}",
            "https://cognito-identity.${AWS::Region}.${AWS::URLSuffix}",
            "https://ssm.${AWS::Region}.${AWS::URLSuffix}",
            "https://sync-states.${AWS::Region}.${AWS::URLSuffix}",
            "https://translate.${AWS::Region}.${AWS::URLSuffix}",
            S3,
            "https://registry.modelcontextprotocol.io",
            "wss://*.cloudfront.net",
            "wss://*.lambda-microvm.${AWS::Region}.on.aws",
        ]
    ),
    "object-src": "'none'",
    "base-uri": "'none'",
    "form-action": "'self'",
    "frame-ancestors": "'self'",
}

# The AWS SDK clients the UI constructs, and the service host each one calls.
SDK_CLIENT_HOSTS = {
    "SSMClient": "https://ssm.${AWS::Region}.${AWS::URLSuffix}",
    # StartSyncExecution, the only Step Functions operation the UI sends, has the
    # endpoint host prefix "sync-" (test_the_ui_sends_step_functions_only_sync_executions).
    "SFNClient": "https://sync-states.${AWS::Region}.${AWS::URLSuffix}",
    "TranslateClient": "https://translate.${AWS::Region}.${AWS::URLSuffix}",
}

SECURITY_HEADERS = {
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "X-Content-Type-Options": "nosniff",
    "Cross-Origin-Resource-Policy": "same-origin",
}

# CloudFront's limit on a Content-Security-Policy value.
CSP_MAX_LENGTH = 1783

PERMISSIONS_POLICY = (
    "camera=(self), microphone=(self), display-capture=(self), geolocation=(), payment=(), "
    "usb=(), serial=(), hid=(), bluetooth=(), midi=()"
)
FRAME_OPTIONS = {"Override": True, "FrameOption": "DENY"}


def evaluate(node, parameters: dict[str, str]):
    """Evaluate the intrinsics the headers policy uses, for the given AllowedEmbedOrigins value."""
    if isinstance(node, dict) and len(node) == 1:
        (name, args), = node.items()
        allowed = parameters["AllowedEmbedOrigins"] != ""
        if name == "Fn::If":
            condition, when_true, when_false = args
            assert condition == "ShouldAllowEmbedOrigins", condition
            return evaluate(when_true if allowed else when_false, parameters)
        if name == "Ref" and args in parameters:
            return parameters[args]
        if name == "Ref" and args == "AWS::NoValue":
            return NO_VALUE
        if name == "Fn::Join":
            separator, items = args
            return separator.join(evaluate(items, parameters))
        if name == "Fn::Split":
            separator, value = args
            return evaluate(value, parameters).split(separator)
    if isinstance(node, list):
        return [evaluate(item, parameters) for item in node]
    if isinstance(node, dict):
        return {key: value for key, value in ((k, evaluate(v, parameters)) for k, v in node.items()) if value is not NO_VALUE}
    return node


NO_VALUE = object()


def render_csp(csp_sub: list, origins: str) -> str:
    """The Fn::Sub template string with EmbedAncestors substituted; AWS and AppSync variables stay."""
    template, variables = csp_sub
    ancestors = evaluate(variables["EmbedAncestors"], {"AllowedEmbedOrigins": origins})
    assert isinstance(ancestors, str)
    return template.replace("${EmbedAncestors}", ancestors)


def parse_csp(value: str) -> dict[str, str]:
    directives = {}
    for part in value.split(";"):
        if part.strip():
            name, _, sources = part.strip().partition(" ")
            assert name not in directives, f"{name} is declared twice"
            directives[name] = " ".join(sources.split())
    return directives


def headers_for(policy: dict, csp_sub: list, origins: str) -> dict[str, str]:
    """The headers the policy adds with AllowedEmbedOrigins = ``origins`` (CSP left as a Sub template)."""
    config = evaluate(policy, {"AllowedEmbedOrigins": origins})
    security = config["SecurityHeadersConfig"]
    headers = {"Content-Security-Policy": render_csp(csp_sub, origins)}
    if "FrameOptions" in security:
        headers["X-Frame-Options"] = security["FrameOptions"]["FrameOption"]
    for item in config["CustomHeadersConfig"]["Items"]:
        headers[item["Header"]] = item["Value"]
    return headers


def _render_long_deployment(csp: str) -> str:
    """``csp`` for the longest Region name, a China URL suffix and AppSync hosts of the length AppSync generates."""
    return (
        csp.replace("${AWS::Region}", "ap-southeast-5")
        .replace("${AWS::URLSuffix}", "amazonaws.com.cn")
        .replace("${AppSyncApiEncrypted.GraphQLDns}", f"{'a' * 26}.appsync-api.ap-southeast-5.amazonaws.com.cn")
        .replace("${AppSyncApiEncrypted.RealtimeDns}", f"{'a' * 26}.appsync-realtime-api.ap-southeast-5.amazonaws.com.cn")
    )


@pytest.fixture(scope="module")
def resources() -> dict:
    return flattened()["Resources"]


@pytest.fixture(scope="module")
def policy(resources) -> dict:
    return resources["SecurityHeadersPolicy"]["Properties"]["ResponseHeadersPolicyConfig"]


@pytest.fixture(scope="module")
def csp_sub(policy) -> list:
    value = policy["SecurityHeadersConfig"]["ContentSecurityPolicy"]["ContentSecurityPolicy"]
    assert set(value) == {"Fn::Sub"} and isinstance(value["Fn::Sub"], list) and len(value["Fn::Sub"]) == 2
    return value["Fn::Sub"]


@pytest.fixture(scope="module")
def csp_template(csp_sub) -> str:
    """The policy as deployed with AllowedEmbedOrigins empty."""
    template, variables = csp_sub
    assert set(variables) == {"EmbedAncestors"}
    return render_csp(csp_sub, "")


@pytest.fixture(scope="module")
def csp(csp_template) -> dict[str, str]:
    return parse_csp(csp_template)


def test_content_security_policy_directives(csp) -> None:
    assert csp == EXPECTED_CSP


def test_scripts_load_from_this_origin_only(csp) -> None:
    assert csp["script-src"] == "'self'"


@pytest.mark.parametrize("directive", sorted(EXPECTED_CSP))
def test_no_directive_allows_a_whole_scheme_or_any_host(csp, directive) -> None:
    for source in csp[directive].split():
        assert source not in {"*", "https:", "http:", "wss:", "ws:"}, f"{directive} allows {source}"
        assert not source.startswith(("http:", "ws:")), f"{directive} allows the cleartext source {source}"
        assert not re.fullmatch(r"(https|wss)://\*\.(amazonaws\.com|\$\{AWS::URLSuffix\})", source), (
            f"{directive} allows every AWS host ({source})"
        )


def test_connect_src_keeps_the_transcriber_websocket(csp) -> None:
    # The WebSocket transcriber is on its own CloudFront distribution, created
    # after this stack, so it can only be named by suffix. Without it Stream
    # Audio/Video and the stream-audio embeds cannot connect.
    assert "wss://*.cloudfront.net" in csp["connect-src"].split()


def test_connect_src_keeps_appsync_and_cognito(csp) -> None:
    sources = csp["connect-src"].split()
    for source in (
        "https://${AppSyncApiEncrypted.GraphQLDns}",
        "wss://${AppSyncApiEncrypted.RealtimeDns}",
        "https://cognito-idp.${AWS::Region}.${AWS::URLSuffix}",
        "https://cognito-identity.${AWS::Region}.${AWS::URLSuffix}",
    ):
        assert source in sources, f"connect-src lacks {source}: sign-in or the GraphQL API would fail"


def test_connect_src_keeps_the_microvm_vnc_endpoint(csp) -> None:
    assert "wss://*.lambda-microvm.${AWS::Region}.on.aws" in csp["connect-src"].split()


def test_connect_src_names_every_sdk_client_the_ui_constructs(csp) -> None:
    sources = csp["connect-src"].split()
    constructed = set()
    for path in (UI / "src").rglob("*.js*"):
        if ".test." in path.name:
            continue
        constructed.update(re.findall(r"new\s+([A-Z][A-Za-z0-9]*Client)\s*\(", path.read_text(encoding="utf-8")))
    unknown = constructed - set(SDK_CLIENT_HOSTS)
    assert not unknown, (
        f"the UI constructs {sorted(unknown)}: add the service's host to connect-src in "
        "SecurityHeadersPolicy and to SDK_CLIENT_HOSTS here"
    )
    for client, host in SDK_CLIENT_HOSTS.items():
        assert host in sources, f"{client} calls {host}, which connect-src does not allow"


def test_the_ui_sends_step_functions_only_sync_executions() -> None:
    # The SDK sends StartSyncExecution to sync-states.<region>, the host the
    # policy names. Any other Step Functions command goes to states.<region>,
    # which would need adding to connect-src.
    commands = set()
    for path in (UI / "src").rglob("*.js*"):
        if ".test." in path.name:
            continue
        text = path.read_text(encoding="utf-8")
        if "@aws-sdk/client-sfn" in text:
            commands.update(re.findall(r"new\s+([A-Z][A-Za-z0-9]*Command)\s*\(", text))
    assert commands == {"StartSyncExecutionCommand"}


def test_content_security_policy_fits_cloudfront_limit(csp_template) -> None:
    rendered = _render_long_deployment(csp_template)
    assert "${" not in rendered
    assert len(rendered) <= CSP_MAX_LENGTH


def test_ui_pages_have_no_inline_script_or_style() -> None:
    pages = [UI / "index.html", *sorted((UI / "public").rglob("*.html"))]
    for page in pages:
        html = page.read_text(encoding="utf-8")
        assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html, re.I), f"{page.name} has an inline script"
        assert not re.search(r"<style\b", html, re.I), f"{page.name} has a style element"
        assert not re.search(r"\son[a-z]+\s*=", html, re.I), f"{page.name} has an inline event handler"


def test_other_security_headers(policy) -> None:
    security = policy["SecurityHeadersConfig"]
    assert security["StrictTransportSecurity"] == {
        "Override": True,
        "AccessControlMaxAgeSec": 31536000,
        "IncludeSubdomains": True,
        "Preload": False,
    }
    assert security["ContentTypeOptions"] == {"Override": True}
    assert security["FrameOptions"] == {
        "Fn::If": ["ShouldAllowEmbedOrigins", {"Ref": "AWS::NoValue"}, FRAME_OPTIONS]
    }
    assert security["ReferrerPolicy"] == {"Override": True, "ReferrerPolicy": "strict-origin-when-cross-origin"}


def test_custom_headers(policy) -> None:
    headers = {item["Header"]: item for item in policy["CustomHeadersConfig"]["Items"]}
    assert headers == {
        "Permissions-Policy": {
            "Header": "Permissions-Policy",
            "Value": PERMISSIONS_POLICY,
            "Override": True,
        },
        "Cross-Origin-Resource-Policy": {
            "Header": "Cross-Origin-Resource-Policy",
            "Value": "same-origin",
            "Override": True,
        },
    }


def test_capture_features_are_allowed_for_this_origin(policy) -> None:
    value = next(i["Value"] for i in policy["CustomHeadersConfig"]["Items"] if i["Header"] == "Permissions-Policy")
    features = dict(part.strip().split("=", 1) for part in value.split(","))
    # Stream Audio/Video captures the microphone and a shared tab, window or screen.
    for feature in ("microphone", "display-capture", "camera"):
        assert features[feature] == "(self)", feature


def test_opener_policy_is_not_sent(policy) -> None:
    # The MCP server OAuth popup and an embed opened as a popup post their
    # result to window.opener, which a browser drops when the popup loads a
    # document that sends Cross-Origin-Opener-Policy.
    names = {item["Header"].lower() for item in policy["CustomHeadersConfig"]["Items"]}
    assert "cross-origin-opener-policy" not in names


def test_origin_server_header_is_removed(policy) -> None:
    assert policy["RemoveHeadersConfig"]["Items"] == [{"Header": "Server"}]


def test_policy_applies_to_the_web_app(resources) -> None:
    behavior = resources["WebAppCloudFrontDistribution"]["Properties"]["DistributionConfig"]["DefaultCacheBehavior"]
    assert behavior["ResponseHeadersPolicyId"] == {"Ref": "SecurityHeadersPolicy"}


EMBED_ORIGINS = "https://crm.example.com,https://*.intranet.example.com:8443"


def _parameter(template_path: Path) -> dict:
    return load(template_path)["Parameters"]["AllowedEmbedOrigins"]


def test_headers_with_no_embed_origins(policy, csp_sub) -> None:
    headers = headers_for(policy, csp_sub, "")
    assert parse_csp(headers["Content-Security-Policy"]) == EXPECTED_CSP
    assert headers["Content-Security-Policy"].endswith("; frame-ancestors 'self'")
    assert headers["X-Frame-Options"] == "DENY"
    assert headers["Permissions-Policy"] == PERMISSIONS_POLICY


def test_headers_with_embed_origins(policy, csp_sub) -> None:
    headers = headers_for(policy, csp_sub, EMBED_ORIGINS)
    ancestors = "'self' https://crm.example.com https://*.intranet.example.com:8443"
    assert parse_csp(headers["Content-Security-Policy"]) == dict(EXPECTED_CSP, **{"frame-ancestors": ancestors})
    # The same policy, with the origins appended to the last directive.
    assert headers["Content-Security-Policy"] == (
        render_csp(csp_sub, "") + " https://crm.example.com https://*.intranet.example.com:8443"
    )
    # X-Frame-Options cannot name the origins, so it is not sent.
    assert "X-Frame-Options" not in headers
    # The embed's own origin is still "self" inside another origin's iframe;
    # the embedding page grants the features through its iframe's allow attribute.
    assert headers["Permissions-Policy"] == PERMISSIONS_POLICY


def test_one_embed_origin(policy, csp_sub) -> None:
    headers = headers_for(policy, csp_sub, "https://crm.example.com")
    assert parse_csp(headers["Content-Security-Policy"])["frame-ancestors"] == "'self' https://crm.example.com"
    assert "X-Frame-Options" not in headers


def test_ai_stack_declares_the_parameter_as_the_main_stack_does() -> None:
    main, ai = _parameter(MAIN_TEMPLATE), _parameter(TEMPLATE)
    for key in ("Type", "Default", "MaxLength", "AllowedPattern", "ConstraintDescription"):
        assert ai[key] == main[key], key
    assert main["Type"] == "String" and main["Default"] == ""


def test_main_stack_passes_the_parameter_to_the_ai_stack() -> None:
    resources = load(MAIN_TEMPLATE)["Resources"]
    passing = [
        name
        for name, resource in resources.items()
        if ((resource.get("Properties") or {}).get("Parameters") or {}).get("AllowedEmbedOrigins")
        == {"Ref": "AllowedEmbedOrigins"}
    ]
    assert passing == ["AISTACK"]


def test_main_stack_shows_the_parameter_with_the_cloudfront_settings() -> None:
    interface = load(MAIN_TEMPLATE)["Metadata"]["AWS::CloudFormation::Interface"]
    group = next(g for g in interface["ParameterGroups"] if "AllowedEmbedOrigins" in g["Parameters"])
    assert group["Label"]["default"] == "Amazon CloudFront Configuration"
    assert interface["ParameterLabels"]["AllowedEmbedOrigins"]["default"]


@pytest.mark.parametrize(
    "value",
    [
        "",
        "https://crm.example.com",
        "https://crm.example.com,https://portal.example.org:8443",
        "https://*.example.com",
        "https://*.intranet.example.com:8443",
        "https://localhost:3000",
        "https://10.0.0.5",
        "https://my-app.example.co.uk",
    ],
)
def test_pattern_accepts(value) -> None:
    assert re.fullmatch(_parameter(MAIN_TEMPLATE)["AllowedPattern"], value)


@pytest.mark.parametrize(
    "value",
    [
        "http://crm.example.com",
        "crm.example.com",
        "https://crm.example.com/",
        "https://crm.example.com/app",
        "https://crm.example.com, https://portal.example.org",
        "https://crm.example.com,",
        ",https://crm.example.com",
        "https://*",
        "https://*.com",
        "*.example.com",
        "https://a.*.example.com",
        "https://Crm.Example.com",
        "https://crm.example.com:",
        "https://-crm.example.com",
        "https://crm.example.com;script-src *",
        "https://crm.example.com 'unsafe-inline'",
        "'self'",
        "*",
        "https:",
        "wss://crm.example.com",
    ],
)
def test_pattern_refuses(value) -> None:
    assert not re.fullmatch(_parameter(MAIN_TEMPLATE)["AllowedPattern"], value)


def _origins_of_length(length: int) -> str:
    """A valid AllowedEmbedOrigins value exactly ``length`` characters long."""
    origins: list[str] = []
    while len(",".join([*origins, "https://e00.example.com"])) + len(",https://p.example.com") <= length:
        origins.append(f"https://e{len(origins):02d}.example.com")
    padding = length - len(",".join(origins)) - len(",https://.example.com")
    value = ",".join([*origins, f"https://{'p' * padding}.example.com"])
    assert len(value) == length
    return value


def test_content_security_policy_with_the_longest_origin_list_fits_cloudfront_limit(policy, csp_sub) -> None:
    max_length = _parameter(MAIN_TEMPLATE)["MaxLength"]
    origins = _origins_of_length(max_length)
    assert re.fullmatch(_parameter(MAIN_TEMPLATE)["AllowedPattern"], origins)
    rendered = _render_long_deployment(headers_for(policy, csp_sub, origins)["Content-Security-Policy"])
    assert "${" not in rendered
    assert len(rendered) <= CSP_MAX_LENGTH, (
        f"with {max_length} characters of origins the policy is {len(rendered)} characters: "
        "lower AllowedEmbedOrigins' MaxLength in both templates, or shorten the policy"
    )


def _lambda_response_headers() -> dict[str, str]:
    tree = ast.parse(MCP_INDEX.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "RESPONSE_HEADERS" for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError("mcp_analytics/index.py defines no RESPONSE_HEADERS")


def test_mcp_function_sends_security_headers() -> None:
    headers = _lambda_response_headers()
    for name, value in SECURITY_HEADERS.items():
        assert headers.get(name) == value, name


@pytest.mark.parametrize("response_type", ["DEFAULT_4XX", "DEFAULT_5XX"])
def test_mcp_gateway_responses_send_security_headers(resources, response_type) -> None:
    responses = [
        r
        for r in resources.values()
        if r["Type"] == "AWS::ApiGateway::GatewayResponse"
        and r["Properties"]["RestApiId"] == {"Ref": "MCPApiKeyRestApi"}
        and r["Properties"]["ResponseType"] == response_type
    ]
    assert len(responses) == 1, f"one {response_type} gateway response on the MCP API"
    params = responses[0]["Properties"]["ResponseParameters"]
    assert params == {f"gatewayresponse.header.{name}": f"'{value}'" for name, value in SECURITY_HEADERS.items()}
    assert responses[0]["Condition"] == "ShouldEnableMCPServer"


def test_mcp_stage_deployment_follows_the_gateway_responses(resources) -> None:
    stage = resources["MCPApiKeyStage"]["Properties"]
    deployment_id = stage["DeploymentId"]["Ref"]
    deployment = resources[deployment_id]
    assert deployment["Type"] == "AWS::ApiGateway::Deployment"
    gateway_responses = {
        name
        for name, r in resources.items()
        if r["Type"] == "AWS::ApiGateway::GatewayResponse"
        and r["Properties"]["RestApiId"] == {"Ref": "MCPApiKeyRestApi"}
    }
    # The deployment is created after the gateway responses, so it includes them.
    assert gateway_responses <= set(deployment["DependsOn"])
