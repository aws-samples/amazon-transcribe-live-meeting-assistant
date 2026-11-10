# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""What a deployment's logs record about an MCP API key, end to end.

Signs in as the Cognito test user, generates an MCP API key through AppSync,
calls the API-key MCP endpoint with it (``x-api-key`` and ``Authorization:
Bearer``) and with a well-formed key that does not exist, then revokes the key
and reads back the two log groups the key passes through:

- the MCP API key authorizer's Lambda log group, and
- the AppSync API's log group (``/aws/appsync/apis/<apiId>``), for the
  ``generateMCPApiKey`` call that returned the key.

The result is a dict of counts and flags only. The key value stays inside
``run_probe`` and is never returned, printed, or put in an exception message,
so a failing assertion cannot show it. It has no command-line entry point and
the tests do not print its result; run it through
``pytest integ-tests/test_mcp_api_key_logging.py --stack-name <stack>``.
See docs/mcp-api-key-auth.md.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
import uuid
from typing import Any

import boto3
import cognito_test_user
from ws_stream_probe import _mint_tokens, _outputs, _stack_resources

# Same length as the KeyPrefix the key manager stores and lists.
KEY_PREFIX_LENGTH = 12
# CloudWatch Logs delivery is usually seconds; allow for a slow minute.
LOG_WAIT_SECONDS = 180
# Log events are stamped with the time they were written, so the window around
# the calls only needs to cover clock skew between this host and AWS.
WINDOW_SLACK_MS = 15_000

LIST_KEYS = "query { listMCPApiKeys { keyPrefix createdAt enabled } }"
GENERATE_KEY = "mutation { generateMCPApiKey { keyValue keyPrefix createdAt } }"
REVOKE_KEY = "mutation Revoke($p: String!) { revokeMCPApiKey(keyPrefix: $p) }"
TOOLS_LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}


class ProbeError(RuntimeError):
    """A step of the probe did not work. Messages never carry a key value."""


def _graphql_url(outputs: dict) -> str:
    for part in outputs.get("LocalUITestingEnv", "").split():
        if part.startswith("VITE_APPSYNC_GRAPHQL_URL="):
            return part.split("=", 1)[1]
    raise ProbeError("stack has no VITE_APPSYNC_GRAPHQL_URL in its LocalUITestingEnv output")


def _post(url: str, headers: dict, body: dict) -> tuple[int, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:  # nosec B310 - https stack endpoint
            raw = response.read()
            status = response.status
    except urllib.error.HTTPError as err:
        raw = err.read()
        status = err.code
    try:
        return status, json.loads(raw or b"null")
    except json.JSONDecodeError:
        return status, None


def _graphql(url: str, token: str, query: str, variables: dict | None = None) -> dict:
    status, body = _post(url, {"Authorization": token}, {"query": query, "variables": variables or {}})
    if status != 200 or not isinstance(body, dict) or body.get("errors"):
        # The error text, not the body: a successful generate's body holds the key.
        errors = body.get("errors") if isinstance(body, dict) else None
        raise ProbeError(f"GraphQL call failed (HTTP {status}): {json.dumps(errors)[:400]}")
    return body["data"]


def _now_ms() -> int:
    return int(time.time() * 1000)


def _events(logs, group: str, start_ms: int, end_ms: int, pattern: str | None = None) -> list[str]:
    kwargs: dict[str, Any] = {"logGroupName": group, "startTime": start_ms, "endTime": end_ms}
    if pattern:
        kwargs["filterPattern"] = pattern
    messages = []
    for page in logs.get_paginator("filter_log_events").paginate(**kwargs):
        messages.extend(e["message"] for e in page.get("events", []))
    return messages


def _appsync_api_id(ai_resources: dict) -> str:
    # Ref on AWS::AppSync::GraphQLApi is the API's ARN: arn:...:apis/<apiId>
    arn = ai_resources["AppSyncApiEncrypted"]
    return arn.rsplit("/", 1)[-1]


def run_probe(stack_name: str, region: str | None = None) -> dict:
    """Run the key lifecycle and report what the logs hold. Counts and flags only."""
    credentials = cognito_test_user.resolve(region)
    if credentials is None:
        raise cognito_test_user.CredentialsUnavailable(
            f"no Cognito test user configured — {cognito_test_user.describe_sources()}"
        )
    username, password = credentials

    session = boto3.Session(region_name=region)
    cfn = session.client("cloudformation")
    logs = session.client("logs")
    outputs = _outputs(cfn, stack_name)
    endpoint = outputs.get("MCPServerApiKeyEndpoint")
    if not endpoint:
        return {"enabled": False}

    ai_stack = next(
        r["PhysicalResourceId"] for r in _stack_resources(cfn, stack_name) if r["LogicalResourceId"] == "AISTACK"
    )
    ai = {r["LogicalResourceId"]: r["PhysicalResourceId"] for r in _stack_resources(cfn, ai_stack)}
    authorizer_group = ai["MCPApiKeyAuthorizerLogGroup"]
    api_id = _appsync_api_id(ai)
    appsync_group = f"/aws/appsync/apis/{api_id}"
    log_config = session.client("appsync").get_graphql_api(apiId=api_id)["graphqlApi"].get("logConfig") or {}

    pool_id = outputs.get("MCPServerUserPoolId") or outputs["CognitoUserPoolTokenIssuerUrl"].rsplit("/", 1)[-1]
    tokens = _mint_tokens(session.region_name, pool_id, outputs["CognitoUserPoolClientId"], username, password)
    graphql_url = _graphql_url(outputs)
    id_token = tokens["id"]

    observed: dict[str, Any] = {
        "enabled": True,
        "appsync_exclude_verbose_content": bool(log_config.get("excludeVerboseContent")),
        "appsync_field_log_level": log_config.get("fieldLogLevel"),
    }

    existing = _graphql(graphql_url, id_token, LIST_KEYS)["listMCPApiKeys"] or []
    if existing:
        # One key per user. The test user's key is left alone rather than revoked,
        # because the test cannot tell whether someone relies on it.
        observed["existing_key_prefixes"] = [k["keyPrefix"] for k in existing]
        return observed

    generate_start = _now_ms()
    generated = _graphql(graphql_url, id_token, GENERATE_KEY)["generateMCPApiKey"]
    generate_end = _now_ms()
    key = generated["keyValue"]
    prefix = generated["keyPrefix"]
    unknown_key = f"lma_{uuid.uuid4()}"
    observed["key_prefix_matches_key"] = key[:KEY_PREFIX_LENGTH] == prefix

    try:
        calls_start = _now_ms()
        observed["status_x_api_key"], _ = _post(endpoint, {"x-api-key": key}, TOOLS_LIST)
        observed["status_bearer"], _ = _post(endpoint, {"Authorization": f"Bearer {key}"}, TOOLS_LIST)
        observed["status_unknown_key"], _ = _post(endpoint, {"x-api-key": unknown_key}, TOOLS_LIST)
        calls_end = _now_ms()
    finally:
        _graphql(graphql_url, id_token, REVOKE_KEY, {"p": prefix})
        remaining = _graphql(graphql_url, id_token, LIST_KEYS)["listMCPApiKeys"] or []
        observed["revoked"] = not any(k["keyPrefix"] == prefix for k in remaining)

    # Wait until the authorizer's entries for both calls have arrived, so that
    # "no key found" is a statement about entries that exist rather than about
    # delivery lag. AppSync is read in the same pass; at its default
    # AppSyncFieldLogLevel (ERROR) a successful call writes no entry at all.
    auth_window = (calls_start - WINDOW_SLACK_MS, calls_end + WINDOW_SLACK_MS)
    gen_window = (generate_start - WINDOW_SLACK_MS, generate_end + WINDOW_SLACK_MS)
    deadline = time.monotonic() + LOG_WAIT_SECONDS
    authorizer_messages: list[str] = []
    appsync_messages: list[str] = []
    while True:
        authorizer_messages = _events(logs, authorizer_group, *auth_window)
        appsync_messages = _events(logs, appsync_group, *gen_window)
        with_prefix = sum(prefix in m for m in authorizer_messages)
        generate_entries = [m for m in appsync_messages if "generateMCPApiKey" in m]
        if with_prefix >= 2 or time.monotonic() >= deadline:
            break
        time.sleep(10)

    observed["authorizer_entries"] = len(authorizer_messages)
    observed["authorizer_entries_with_prefix"] = with_prefix
    observed["authorizer_entries_with_key"] = sum(key in m for m in authorizer_messages)
    observed["authorizer_entries_with_unknown_key"] = sum(unknown_key in m for m in authorizer_messages)
    observed["appsync_generate_entries"] = len(generate_entries)
    observed["appsync_entries_with_key"] = sum(key in m for m in appsync_messages)
    observed["appsync_generate_entries_with_template"] = sum(
        _has_verbose_content(m) for m in generate_entries
    )
    return observed


def _has_verbose_content(message: str) -> bool:
    """Whether an AppSync log entry carries the resolver context or evaluated template."""
    try:
        entry = json.loads(message)
    except json.JSONDecodeError:
        return False
    return bool(entry.get("transformedTemplate") or entry.get("context"))

