"""
Unit tests for MCP API Key Authorizer Lambda.
Tests token extraction from various header formats, SHA-256 hashing,
DynamoDB lookup, and IAM policy generation.
"""

import hashlib
import os
import unittest
from unittest.mock import MagicMock, patch

# Set env vars before importing
os.environ["MCP_API_KEYS_TABLE"] = "test-mcp-api-keys"  # pragma: allowlist secret
os.environ["LOG_LEVEL"] = "WARNING"

import index

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
            result = index.handler(event, None)
            self.assertEqual(result["policyDocument"]["Statement"][0]["Effect"], "Deny")
        finally:
            index.TABLE_NAME = original


class TestAllowPolicy(unittest.TestCase):
    """Test the Allow policy structure."""

    @patch.object(index, "dynamodb")
    def test_allow_policy_structure(self, mock_dynamo):
        """Verify Allow policy has correct ARN pattern and user context."""
        token = "lma_policy-test"
        item = make_valid_item(token)
        item["IsAdmin"] = "true"
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


if __name__ == "__main__":
    unittest.main()
