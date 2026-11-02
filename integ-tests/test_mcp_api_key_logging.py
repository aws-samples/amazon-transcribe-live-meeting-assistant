# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""MCP API keys in the logs: the authorizer and AppSync never record a key.

One probe run (mcp_api_key_probe.py) generates a key as the Cognito test user,
calls the API-key MCP endpoint with it and with an unknown key, revokes it, and
reads back the authorizer's and the AppSync API's log entries for those calls.
Each test asserts one property of what it observed. The probe returns counts
and flags only, so no assertion here can show a key.

Runs whenever a Cognito test user is configured (as the scheduled run does),
like the other credentialed tests in test_lma_integration.py, and skips without
one. On a stack deployed with ``EnableMCP=false`` it skips, marked
``optional_feature`` so that ``--no-skips`` does not turn it into a failure.

Self-cleaning: the key it generates is revoked in a ``finally``. If the test
user already holds a key it is left alone and the test fails, naming the key's
prefix, because one user can hold only one key.
"""

from __future__ import annotations

import cognito_test_user
import pytest


@pytest.fixture(scope="module")
def observed(client, request):
    if not cognito_test_user.available(client.region):
        pytest.skip(f"no Cognito test user — {cognito_test_user.describe_sources()}")
    try:
        import pycognito  # noqa: F401
    except ImportError:
        pytest.skip("pycognito not installed (pip install -r integ-tests/requirements.txt)")

    from mcp_api_key_probe import run_probe

    result = run_probe(client.stack_name, region=client.region)
    if not result["enabled"]:
        # Off by default is not a gap, so --no-skips leaves this skip alone.
        request.node.add_marker(pytest.mark.optional_feature)
        pytest.skip(f"{client.stack_name} has no MCP API key endpoint (EnableMCP=false)")
    if result.get("existing_key_prefixes"):
        pytest.fail(
            f"the Cognito test user already holds an MCP API key (prefix "
            f"{result['existing_key_prefixes']}); revoke it from the MCP API Keys page and re-run"
        )
    return result


def test_generated_key_is_accepted_and_revoked(observed) -> None:
    assert observed["key_prefix_matches_key"], "keyPrefix is not the first 12 characters of the generated key"
    assert observed["status_x_api_key"] == 200, f"x-api-key call returned HTTP {observed['status_x_api_key']}"
    assert observed["status_bearer"] == 200, f"Bearer call returned HTTP {observed['status_bearer']}"
    assert observed["status_unknown_key"] in (401, 403), (
        f"unknown key returned HTTP {observed['status_unknown_key']}, expected 401/403"
    )
    assert observed["revoked"], "the generated key is still listed after revokeMCPApiKey"


def test_authorizer_log_records_key_prefix(observed) -> None:
    assert observed["authorizer_entries_with_prefix"] >= 2, (
        f"expected authorizer log entries naming the key prefix for both calls, found "
        f"{observed['authorizer_entries_with_prefix']} of {observed['authorizer_entries']} entries; "
        f"without them the checks below have nothing to inspect"
    )


def test_authorizer_log_omits_api_key(observed) -> None:
    hits = observed["authorizer_entries_with_key"]
    assert hits == 0, f"{hits} authorizer log entries contain the full API key"


def test_authorizer_log_omits_unknown_api_key(observed) -> None:
    hits = observed["authorizer_entries_with_unknown_key"]
    assert hits == 0, f"{hits} authorizer log entries contain the full unknown API key"


def test_appsync_logging_excludes_verbose_content(observed) -> None:
    assert observed["appsync_exclude_verbose_content"], (
        f"AppSync logConfig.excludeVerboseContent is false (fieldLogLevel "
        f"{observed['appsync_field_log_level']}); the stack's AppSyncFieldLogLevel is set to a "
        f"diagnostic level (DEBUG or ALL), which logs request content"
    )


def test_appsync_log_omits_generated_api_key(observed) -> None:
    # At the default field log level a successful call writes no entry, so the
    # count of generateMCPApiKey entries may be zero; any that exist are checked.
    hits = observed["appsync_entries_with_key"]
    assert hits == 0, f"{hits} AppSync log entries contain the generated API key"
    templated = observed["appsync_generate_entries_with_template"]
    assert templated == 0, (
        f"{templated} of {observed['appsync_generate_entries']} generateMCPApiKey log entries "
        f"carry the resolver context or template"
    )
