# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Which roles the hosted MCP server answers, on both of its entry paths.

The API key endpoint's authorizer reads the key owner's role and passes it in
its context; the OAuth gateway forwards the token's cognito:groups claim. Each
path refuses a role the role matrix does not list for MCP access before any tool
runs, and still answers an Admin or a User (including a context from an
authorizer that names no role).
"""

import importlib.util
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

os.environ["LOG_LEVEL"] = "WARNING"
os.environ.setdefault("GRAPHQL_API_ENDPOINT", "https://test.appsync.amazonaws.com/graphql")
os.environ.setdefault("MEETINGS_TABLE_NAME", "test-meetings")
os.environ.setdefault("APPSYNC_GRAPHQL_URL", "https://test.appsync.amazonaws.com/graphql")
os.environ.setdefault("LMA_WEB_APP_URL", "https://test-lma.example.com")

TOOL_MODULES = [
    "tools",
    "tools.search_meetings",
    "tools.get_transcript",
    "tools.get_summary",
    "tools.list_meetings",
    "tools.schedule_meeting",
    "tools.start_meeting_now",
]


def _load_index():
    """This directory's index.py under a name of its own, with its tools mocked.

    The mocks are removed again afterwards, so test_jsonrpc.py, collected in the
    same run, installs and patches its own.
    """
    saved = {name: sys.modules.get(name) for name in TOOL_MODULES}
    for name in TOOL_MODULES:
        sys.modules[name] = MagicMock()
    try:
        spec = importlib.util.spec_from_file_location(
            "mcp_analytics_index_roles", Path(__file__).resolve().parent / "index.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        for name, original in saved.items():
            if original is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original


index = _load_index()

ROLE_MATRIX = Path(__file__).resolve().parents[2] / "appsync" / "role-matrix.json"
LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}


def api_event(role=None):
    authorizer = {"userId": "someone", "username": "someone@example.com", "isAdmin": "false"}
    if role is not None:
        authorizer["role"] = role
    return {
        "httpMethod": "POST",
        "body": json.dumps(LIST),
        "requestContext": {"authorizer": authorizer},
    }


def gateway_event(groups):
    claims = {"sub": "a1b2", "cognito:username": "someone@example.com"}
    if groups is not None:
        claims["cognito:groups"] = groups
    return {"requestContext": {"authorizer": {"claims": claims}}}


def gateway_context():
    context = MagicMock()
    context.client_context.custom = {"bedrockAgentCoreToolName": "target___list_meetings"}
    return context


class TestRoleRule(unittest.TestCase):
    def test_the_role_rule_matches_the_role_matrix(self):
        for vector in json.loads(ROLE_MATRIX.read_text())["effectiveRole"]["vectors"]:
            with self.subTest(groups=vector["groups"]):
                self.assertEqual(index.effective_role(vector["groups"]), vector["role"])

    def test_the_admitted_roles_are_those_the_matrix_lists_for_both_endpoints(self):
        capabilities = json.loads(ROLE_MATRIX.read_text())["capabilities"]
        for surface in ("mcp-api-key-endpoint", "mcp-oauth-gateway"):
            listed = next(c for c in capabilities if surface in c["surfaces"])
            self.assertEqual(sorted(index.MCP_ROLES), sorted(listed["roles"]), surface)


class TestApiKeyPath(unittest.TestCase):
    def test_a_participant_role_in_the_authorizer_context_is_refused(self):
        result = index.lambda_handler(api_event("Participant"), None)

        self.assertEqual(result["statusCode"], 403)
        self.assertIn("role", json.loads(result["body"])["error"]["message"])

    def test_admin_and_user_roles_are_answered(self):
        for role in ("Admin", "User", None):
            with self.subTest(role=role):
                self.assertEqual(index.lambda_handler(api_event(role), None)["statusCode"], 200)


class TestGatewayPath(unittest.TestCase):
    def test_a_participant_token_is_refused(self):
        for groups in (["Participant"], "Participant", ["Sales", "Participant"]):
            with self.subTest(groups=groups):
                result = index.lambda_handler(gateway_event(groups), gateway_context())
                self.assertEqual(result["statusCode"], 403)
                self.assertIn("role", json.loads(result["body"])["error"]["message"])

    def test_an_admin_who_is_also_a_participant_is_not_refused_for_the_role(self):
        result = index.lambda_handler(gateway_event(["Participant", "Admin"]), gateway_context())

        self.assertNotIn(index.ROLE_REFUSAL_MESSAGE, result.get("body", ""))

    def test_a_user_token_is_not_refused_for_the_role(self):
        for groups in (["User"], None):
            with self.subTest(groups=groups):
                result = index.lambda_handler(gateway_event(groups), gateway_context())
                self.assertNotIn(index.ROLE_REFUSAL_MESSAGE, result.get("body", ""))


if __name__ == "__main__":
    unittest.main()
