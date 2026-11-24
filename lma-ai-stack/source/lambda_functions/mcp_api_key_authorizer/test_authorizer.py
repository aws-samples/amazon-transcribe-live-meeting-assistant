"""
Unit tests for MCP API Key Authorizer Lambda.
Tests token extraction from various header formats, SHA-256 hashing,
DynamoDB lookup, the key owner's current role, and IAM policy generation.
"""

import hashlib
import json
import os
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from botocore.exceptions import ClientError

# Set env vars before importing
os.environ["MCP_API_KEYS_TABLE"] = "test-mcp-api-keys"  # pragma: allowlist secret
os.environ["USER_POOL_ID"] = "us-east-1_testpool"
os.environ["LOG_LEVEL"] = "WARNING"
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

import index

ROLE_MATRIX = Path(__file__).resolve().parents[2] / "appsync" / "role-matrix.json"


class FakeCognito:
    """The user pool, as admin_list_groups_for_user answers for each key owner."""

    def __init__(self):
        self.groups = {}
        self.missing = set()
        self.error = None
        self.page_size = 60
        self.calls = []

    def admin_list_groups_for_user(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        username = kwargs["Username"]
        if username in self.missing:
            raise ClientError(
                {"Error": {"Code": "UserNotFoundException", "Message": "gone"}},
                "AdminListGroupsForUser",
            )
        names = self.groups.get(username, [])
        start = int(kwargs.get("NextToken") or 0)
        page = {"Groups": [{"GroupName": n} for n in names[start : start + self.page_size]]}
        if start + self.page_size < len(names):
            page["NextToken"] = str(start + self.page_size)
        return page


# Every test reads the owner's role through this double; by default an owner is
# in no group, which is a User.
COGNITO = FakeCognito()
index.cognito = COGNITO

METHOD_ARN = "arn:aws:execute-api:us-east-1:123456789012:abc123def/prod/POST/mcp"


def make_valid_item(token="lma_test-key-1234"):
    """Create a valid DynamoDB item for a given token."""
    return {
        "KeyHash": hashlib.sha256(token.encode()).hexdigest(),
        "UserId": "testuser",
        "Username": "testuser@example.com",
        "IsAdmin": "false",
        "KeyPrefix": token[:12],
        "Enabled": "true",
    }


class TestTokenExtraction(unittest.TestCase):
    """Test token extraction from various event formats."""

    @patch.object(index, "dynamodb")
    def test_authorization_token_field(self, mock_dynamo):
        """TOKEN-mode compat: authorizationToken field."""
        token = "lma_test-key-1234"
        mock_table = MagicMock()
        mock_table.get_item.return_value = {"Item": make_valid_item(token)}
        mock_dynamo.Table.return_value = mock_table

        event = {"methodArn": METHOD_ARN, "authorizationToken": token}
        result = index.handler(event, None)

        self.assertEqual(result["policyDocument"]["Statement"][0]["Effect"], "Allow")
        mock_table.get_item.assert_called_once_with(
            Key={"KeyHash": hashlib.sha256(token.encode()).hexdigest()}
        )

    @patch.object(index, "dynamodb")
    def test_bearer_header(self, mock_dynamo):
        """Authorization: Bearer <key> header."""
        token = "lma_bearer-test-key"
        mock_table = MagicMock()
        mock_table.get_item.return_value = {"Item": make_valid_item(token)}
        mock_dynamo.Table.return_value = mock_table

        event = {
            "methodArn": METHOD_ARN,
            "headers": {"Authorization": f"Bearer {token}"},
        }
        result = index.handler(event, None)
        self.assertEqual(result["policyDocument"]["Statement"][0]["Effect"], "Allow")

    @patch.object(index, "dynamodb")
    def test_x_api_key_header(self, mock_dynamo):
        """x-api-key header."""
        token = "lma_xapikey-test"
        mock_table = MagicMock()
        mock_table.get_item.return_value = {"Item": make_valid_item(token)}
        mock_dynamo.Table.return_value = mock_table

        event = {
            "methodArn": METHOD_ARN,
            "headers": {"x-api-key": token},
        }
        result = index.handler(event, None)
        self.assertEqual(result["policyDocument"]["Statement"][0]["Effect"], "Allow")

    @patch.object(index, "dynamodb")
    def test_x_api_key_header_capitalized(self, mock_dynamo):
        """X-Api-Key header (capitalized)."""
        token = "lma_capitalized-key"
        mock_table = MagicMock()
        mock_table.get_item.return_value = {"Item": make_valid_item(token)}
        mock_dynamo.Table.return_value = mock_table

        event = {
            "methodArn": METHOD_ARN,
            "headers": {"X-Api-Key": token},
        }
        result = index.handler(event, None)
        self.assertEqual(result["policyDocument"]["Statement"][0]["Effect"], "Allow")

    @patch.object(index, "dynamodb")
    def test_lowercase_authorization_header(self, mock_dynamo):
        """authorization (lowercase) header."""
        token = "lma_lowercase-auth"
        mock_table = MagicMock()
        mock_table.get_item.return_value = {"Item": make_valid_item(token)}
        mock_dynamo.Table.return_value = mock_table

        event = {
            "methodArn": METHOD_ARN,
            "headers": {"authorization": f"Bearer {token}"},
        }
        result = index.handler(event, None)
        self.assertEqual(result["policyDocument"]["Statement"][0]["Effect"], "Allow")


class TestDenyPolicies(unittest.TestCase):
    """Test cases that should return Deny policies."""

    def test_no_token(self):
        """No token provided at all."""
        event = {"methodArn": METHOD_ARN, "headers": {}}
        result = index.handler(event, None)
        self.assertEqual(result["policyDocument"]["Statement"][0]["Effect"], "Deny")
        self.assertEqual(result["principalId"], "unauthorized")

    def test_empty_token(self):
        """Empty authorizationToken."""
        event = {"methodArn": METHOD_ARN, "authorizationToken": ""}
        result = index.handler(event, None)
        self.assertEqual(result["policyDocument"]["Statement"][0]["Effect"], "Deny")

    @patch.object(index, "dynamodb")
    def test_key_not_found(self, mock_dynamo):
        """Token doesn't match any key in DynamoDB."""
        mock_table = MagicMock()
        mock_table.get_item.return_value = {}  # No Item
        mock_dynamo.Table.return_value = mock_table

        event = {"methodArn": METHOD_ARN, "authorizationToken": "lma_nonexistent"}
        result = index.handler(event, None)
        self.assertEqual(result["policyDocument"]["Statement"][0]["Effect"], "Deny")

    @patch.object(index, "dynamodb")
    def test_disabled_key(self, mock_dynamo):
        """Key exists but is disabled."""
        item = make_valid_item("lma_disabled-key")
        item["Enabled"] = "false"
        mock_table = MagicMock()
        mock_table.get_item.return_value = {"Item": item}
        mock_dynamo.Table.return_value = mock_table

        event = {"methodArn": METHOD_ARN, "authorizationToken": "lma_disabled-key"}
        result = index.handler(event, None)
        self.assertEqual(result["policyDocument"]["Statement"][0]["Effect"], "Deny")

    @patch.object(index, "dynamodb")
    def test_dynamodb_error(self, mock_dynamo):
        """DynamoDB throws an exception."""
        mock_table = MagicMock()
        mock_table.get_item.side_effect = Exception("DynamoDB error")
        mock_dynamo.Table.return_value = mock_table

        event = {"methodArn": METHOD_ARN, "authorizationToken": "lma_error-key"}
        result = index.handler(event, None)
        self.assertEqual(result["policyDocument"]["Statement"][0]["Effect"], "Deny")

    def test_no_table_name(self):
        """TABLE_NAME env var is empty."""
        original = index.TABLE_NAME
        index.TABLE_NAME = ""
        try:
            event = {"methodArn": METHOD_ARN, "authorizationToken": "lma_test"}
            with self.assertLogs(index.logger, level="ERROR") as captured:
                result = index.handler(event, None)
            self.assertEqual(result["policyDocument"]["Statement"][0]["Effect"], "Deny")
            self.assertIn("MCP_API_KEYS_TABLE is not set", "\n".join(captured.output))
            self.assertNotIn("lma_test", "\n".join(captured.output))
        finally:
            index.TABLE_NAME = original


class TestAllowPolicy(unittest.TestCase):
    """Test the Allow policy structure."""

    @patch.object(index, "dynamodb")
    def test_allow_policy_structure(self, mock_dynamo):
        """Verify Allow policy has correct ARN pattern and user context."""
        token = "lma_policy-test"
        item = make_valid_item(token)
        COGNITO.groups["testuser@example.com"] = ["Admin"]
        self.addCleanup(COGNITO.groups.clear)
        mock_table = MagicMock()
        mock_table.get_item.return_value = {"Item": item}
        mock_dynamo.Table.return_value = mock_table

        event = {"methodArn": METHOD_ARN, "authorizationToken": token}
        result = index.handler(event, None)

        self.assertEqual(result["principalId"], "testuser")
        self.assertEqual(result["policyDocument"]["Version"], "2012-10-17")
        stmt = result["policyDocument"]["Statement"][0]
        self.assertEqual(stmt["Effect"], "Allow")
        self.assertEqual(stmt["Action"], "execute-api:Invoke")
        # ARN should use wildcard for stage resources
        self.assertIn("/*", stmt["Resource"])
        # Context should contain user info
        self.assertEqual(result["context"]["userId"], "testuser")
        self.assertEqual(result["context"]["username"], "testuser@example.com")
        self.assertEqual(result["context"]["isAdmin"], "true")

    @patch.object(index, "dynamodb")
    def test_the_context_carries_the_key_scope(self, mock_dynamo):
        """read-write is passed as such, a key stored without a scope as legacy, any other value as read."""
        token = "lma_scope-test"
        mock_table = MagicMock()
        mock_dynamo.Table.return_value = mock_table
        event = {"methodArn": METHOD_ARN, "authorizationToken": token}
        for stored, expected in (
            ("read-write", "read-write"),
            ("read", "read"),
            (None, "legacy"),
            ("admin", "read"),
        ):
            item = make_valid_item(token)
            if stored is not None:
                item["Scope"] = stored
            mock_table.get_item.return_value = {"Item": item}
            result = index.handler(event, None)
            self.assertEqual(result["context"]["scope"], expected, stored)

    def test_deny_policy_with_empty_arn(self):
        """Deny policy should use '*' when methodArn is empty."""
        result = index.deny_policy("")
        self.assertEqual(result["policyDocument"]["Statement"][0]["Resource"], "*")


class TestSHA256Hashing(unittest.TestCase):
    """Verify SHA-256 hashing is correct."""

    @patch.object(index, "dynamodb")
    def test_hash_matches_expected(self, mock_dynamo):
        """Verify the hash sent to DynamoDB matches SHA-256 of the token."""
        token = "lma_12345678-abcd-efgh-ijkl-mnopqrstuvwx"  # pragma: allowlist secret
        expected_hash = hashlib.sha256(token.encode()).hexdigest()

        mock_table = MagicMock()
        mock_table.get_item.return_value = {}
        mock_dynamo.Table.return_value = mock_table

        event = {"methodArn": METHOD_ARN, "authorizationToken": token}
        index.handler(event, None)

        mock_table.get_item.assert_called_once_with(Key={"KeyHash": expected_hash})


FULL_KEY = "lma_0f1e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b"  # pragma: allowlist secret


def request_event(headers):
    """A REQUEST-authorizer event as API Gateway sends it."""
    return {
        "type": "REQUEST",
        "methodArn": METHOD_ARN,
        "resource": "/{proxy+}",
        "path": "/mcp",
        "httpMethod": "POST",
        "headers": headers,
        "multiValueHeaders": {k: [v] for k, v in headers.items()},
        "requestContext": {"requestId": "req-123", "httpMethod": "POST"},
    }


class TestLogOutputOmitsKey(unittest.TestCase):
    """The presented API key never appears in log output; a matched key's stored prefix may."""

    def run_and_capture(self, event, table_behaviour):
        with patch.object(index, "dynamodb") as mock_dynamo:
            mock_table = MagicMock()
            table_behaviour(mock_table)
            mock_dynamo.Table.return_value = mock_table
            with self.assertLogs(index.logger, level="DEBUG") as captured:
                result = index.handler(event, None)
        return result, "\n".join(captured.output)

    def assert_key_not_logged(self, output):
        self.assertNotIn(FULL_KEY, output)
        # Nothing longer than the stored 12-character prefix either.
        self.assertNotIn(FULL_KEY[:13], output)

    def test_allowed_request_logs_only_key_prefix(self):
        def found(table):
            table.get_item.return_value = {"Item": make_valid_item(FULL_KEY)}

        for headers in (
            {"x-api-key": FULL_KEY},
            {"X-Api-Key": FULL_KEY},
            {"Authorization": f"Bearer {FULL_KEY}"},
            {"authorization": f"Bearer {FULL_KEY}"},
        ):
            with self.subTest(header=next(iter(headers))):
                result, output = self.run_and_capture(request_event(headers), found)
                self.assertEqual(result["policyDocument"]["Statement"][0]["Effect"], "Allow")
                self.assert_key_not_logged(output)
                self.assertIn(f"keyPrefix={FULL_KEY[:12]}", output)
                self.assertIn("req-123", output)

    def test_unknown_key_logs_no_part_of_key(self):
        def missing(table):
            table.get_item.return_value = {}

        result, output = self.run_and_capture(request_event({"x-api-key": FULL_KEY}), missing)
        self.assertEqual(result["policyDocument"]["Statement"][0]["Effect"], "Deny")
        self.assertNotIn(FULL_KEY[:12], output)
        self.assertIn("req-123", output)

    def test_lookup_error_logs_no_part_of_key(self):
        def failing(table):
            table.get_item.side_effect = RuntimeError(f"lookup failed for {FULL_KEY}")

        result, output = self.run_and_capture(request_event({"x-api-key": FULL_KEY}), failing)
        self.assertEqual(result["policyDocument"]["Statement"][0]["Effect"], "Deny")
        self.assertNotIn(FULL_KEY[:12], output)
        self.assertIn("RuntimeError", output)

    def test_authorization_token_field_not_logged(self):
        def found(table):
            table.get_item.return_value = {"Item": make_valid_item(FULL_KEY)}

        event = {"methodArn": METHOD_ARN, "authorizationToken": FULL_KEY}
        _, output = self.run_and_capture(event, found)
        self.assert_key_not_logged(output)

    def test_request_without_key_is_denied_and_headers_not_logged(self):
        def unused(table):
            table.get_item.side_effect = AssertionError("not called")

        headers = {"Cookie": "session=abc123secret", "User-Agent": "test"}
        result, output = self.run_and_capture(request_event(headers), unused)
        self.assertEqual(result["policyDocument"]["Statement"][0]["Effect"], "Deny")
        self.assertNotIn("abc123secret", output)

    def test_null_headers_are_denied(self):
        event = request_event({})
        event["headers"] = None
        result = index.handler(event, None)
        self.assertEqual(result["policyDocument"]["Statement"][0]["Effect"], "Deny")


if __name__ == "__main__":
    unittest.main()


class TestOwnerRole(unittest.TestCase):
    """The key owner's role is read from the user pool on every request."""

    @staticmethod
    def reset():
        COGNITO.groups.clear()
        COGNITO.missing.clear()
        COGNITO.error = None
        COGNITO.page_size = 60
        COGNITO.calls.clear()

    def setUp(self):
        self.reset()
        self.addCleanup(self.reset)

    def authorize(self, item=None):
        token = "lma_role-test-key"
        with patch.object(index, "dynamodb") as mock_dynamo:
            mock_table = MagicMock()
            mock_table.get_item.return_value = {"Item": item or make_valid_item(token)}
            mock_dynamo.Table.return_value = mock_table
            return index.handler({"methodArn": METHOD_ARN, "authorizationToken": token}, None)

    def effect(self, result):
        return result["policyDocument"]["Statement"][0]["Effect"]

    def test_the_role_rule_matches_the_role_matrix(self):
        for vector in json.loads(ROLE_MATRIX.read_text())["effectiveRole"]["vectors"]:
            with self.subTest(groups=vector["groups"]):
                self.assertEqual(index.effective_role(vector["groups"]), vector["role"])

    def test_the_admitted_roles_are_those_the_matrix_lists_for_this_endpoint(self):
        capabilities = json.loads(ROLE_MATRIX.read_text())["capabilities"]
        listed = next(c for c in capabilities if "mcp-api-key-endpoint" in c["surfaces"])
        self.assertEqual(sorted(index.MCP_ROLES), sorted(listed["roles"]))

    def test_a_participant_owners_key_is_refused(self):
        COGNITO.groups["testuser@example.com"] = ["Participant"]

        self.assertEqual(self.effect(self.authorize()), "Deny")

    def test_a_key_stored_for_an_admin_is_refused_once_its_owner_is_a_participant(self):
        """The stored IsAdmin is not consulted: the role is the owner's now."""
        item = make_valid_item("lma_role-test-key")
        item["IsAdmin"] = "true"
        COGNITO.groups["testuser@example.com"] = ["Participant"]

        self.assertEqual(self.effect(self.authorize(item)), "Deny")

    def test_a_user_owners_key_is_allowed_without_admin(self):
        result = self.authorize()

        self.assertEqual(self.effect(result), "Allow")
        self.assertEqual(result["context"]["isAdmin"], "false")
        self.assertEqual(result["context"]["role"], "User")

    def test_an_admin_flag_follows_the_owners_current_groups(self):
        item = make_valid_item("lma_role-test-key")
        item["IsAdmin"] = "true"

        result = self.authorize(item)

        self.assertEqual(result["context"]["isAdmin"], "false")

    def test_an_admin_owner_is_an_admin(self):
        COGNITO.groups["testuser@example.com"] = ["Sales", "Admin"]

        result = self.authorize()

        self.assertEqual(result["context"]["isAdmin"], "true")
        self.assertEqual(result["context"]["role"], "Admin")

    def test_groups_on_a_later_page_are_read(self):
        COGNITO.page_size = 1
        COGNITO.groups["testuser@example.com"] = ["Sales", "Emea", "Participant"]

        self.assertEqual(self.effect(self.authorize()), "Deny")
        self.assertEqual(len(COGNITO.calls), 3)

    def test_a_key_whose_owner_no_longer_exists_is_refused(self):
        COGNITO.missing.add("testuser@example.com")

        self.assertEqual(self.effect(self.authorize()), "Deny")

    def test_a_failed_role_lookup_is_refused(self):
        COGNITO.error = ClientError(
            {"Error": {"Code": "TooManyRequestsException", "Message": "slow down"}},
            "AdminListGroupsForUser",
        )

        self.assertEqual(self.effect(self.authorize()), "Deny")

    def test_the_lookup_names_the_configured_pool_and_the_keys_owner(self):
        self.authorize()

        self.assertEqual(COGNITO.calls[0]["UserPoolId"], "us-east-1_testpool")
        self.assertEqual(COGNITO.calls[0]["Username"], "testuser@example.com")

    def test_without_a_user_pool_every_key_is_refused(self):
        original = index.USER_POOL_ID
        index.USER_POOL_ID = ""
        try:
            self.assertEqual(self.effect(self.authorize()), "Deny")
        finally:
            index.USER_POOL_ID = original
