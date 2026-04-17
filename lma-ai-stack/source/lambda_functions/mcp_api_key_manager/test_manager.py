"""
Unit tests for MCP API Key Manager Lambda.
Tests generate/list/revoke API key operations, one-key-per-user enforcement,
and authentication checks.
"""

import hashlib
import os
import unittest
from unittest.mock import MagicMock, patch

# Set env vars before importing
os.environ['MCP_API_KEYS_TABLE'] = 'test-mcp-api-keys'
os.environ['LOG_LEVEL'] = 'WARNING'

import index


def make_event(field_name, username='testuser', groups=None, arguments=None):
    """Create an AppSync resolver event."""
    return {
        'info': {'fieldName': field_name},
        'identity': {
            'username': username,
            'groups': groups or [],
        },
        'arguments': arguments or {},
    }


class TestAuthentication(unittest.TestCase):
    """Test authentication enforcement."""

    def test_unauthenticated_user_rejected(self):
        """Users without username are rejected."""
        event = make_event('generateMCPApiKey', username='')
        with self.assertRaises(Exception) as ctx:
            index.handler(event, None)
        self.assertIn('Not authenticated', str(ctx.exception))

    def test_unknown_operation_rejected(self):
        """Unknown fieldName is rejected."""
        event = make_event('deleteMCPApiKey', username='testuser')
        with self.assertRaises(Exception) as ctx:
            index.handler(event, None)
        self.assertIn('Unknown operation', str(ctx.exception))


class TestGenerateKey(unittest.TestCase):
    """Test API key generation."""

    @patch.object(index, 'dynamodb')
    @patch('index.uuid')
    def test_generate_key_success(self, mock_uuid, mock_dynamo):
        """Generate a new key for a user with no existing key."""
        mock_uuid.uuid4.return_value = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'

        mock_table = MagicMock()
        mock_table.query.return_value = {'Items': []}  # No existing key
        mock_dynamo.Table.return_value = mock_table

        event = make_event('generateMCPApiKey', username='alice')
        result = index.handler(event, None)

        self.assertIn('keyValue', result)
        self.assertTrue(result['keyValue'].startswith('lma_'))
        self.assertIn('keyPrefix', result)
        self.assertEqual(len(result['keyPrefix']), 12)
        self.assertIn('createdAt', result)

        # Verify DynamoDB put was called
        mock_table.put_item.assert_called_once()
        put_args = mock_table.put_item.call_args[1]['Item']
        self.assertEqual(put_args['UserId'], 'alice')
        self.assertEqual(put_args['Enabled'], 'true')
        # Verify key is SHA-256 hashed
        expected_hash = hashlib.sha256(result['keyValue'].encode()).hexdigest()
        self.assertEqual(put_args['KeyHash'], expected_hash)

    @patch.object(index, 'dynamodb')
    def test_generate_key_already_exists(self, mock_dynamo):
        """Reject if user already has a key."""
        mock_table = MagicMock()
        mock_table.query.return_value = {'Items': [{'KeyHash': 'existing'}]}
        mock_dynamo.Table.return_value = mock_table

        event = make_event('generateMCPApiKey', username='alice')
        with self.assertRaises(Exception) as ctx:
            index.handler(event, None)
        self.assertIn('already have an API key', str(ctx.exception))

    @patch.object(index, 'dynamodb')
    @patch('index.uuid')
    def test_admin_flag_set_correctly(self, mock_uuid, mock_dynamo):
        """Admin flag is set based on Cognito groups."""
        mock_uuid.uuid4.return_value = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'
        mock_table = MagicMock()
        mock_table.query.return_value = {'Items': []}
        mock_dynamo.Table.return_value = mock_table

        # Admin user
        event = make_event('generateMCPApiKey', username='admin', groups=['Admin'])
        index.handler(event, None)
        put_args = mock_table.put_item.call_args[1]['Item']
        self.assertEqual(put_args['IsAdmin'], 'true')

        # Non-admin user
        mock_table.reset_mock()
        mock_table.query.return_value = {'Items': []}
        event = make_event('generateMCPApiKey', username='regular', groups=['Users'])
        index.handler(event, None)
        put_args = mock_table.put_item.call_args[1]['Item']
        self.assertEqual(put_args['IsAdmin'], 'false')


class TestRevokeKey(unittest.TestCase):
    """Test API key revocation."""

    @patch.object(index, 'dynamodb')
    def test_revoke_key_success(self, mock_dynamo):
        """Successfully revoke an existing key."""
        mock_table = MagicMock()
        mock_table.query.return_value = {'Items': [
            {'KeyHash': 'hash123', 'KeyPrefix': 'lma_abcdef12'}
        ]}
        mock_dynamo.Table.return_value = mock_table

        event = make_event('revokeMCPApiKey', username='alice',
                           arguments={'keyPrefix': 'lma_abcdef12'})
        result = index.handler(event, None)

        self.assertTrue(result)
        mock_table.delete_item.assert_called_once_with(Key={'KeyHash': 'hash123'})

    @patch.object(index, 'dynamodb')
    def test_revoke_key_not_found(self, mock_dynamo):
        """Reject revoke if prefix doesn't match."""
        mock_table = MagicMock()
        mock_table.query.return_value = {'Items': [
            {'KeyHash': 'hash123', 'KeyPrefix': 'lma_abcdef12'}
        ]}
        mock_dynamo.Table.return_value = mock_table

        event = make_event('revokeMCPApiKey', username='alice',
                           arguments={'keyPrefix': 'lma_wrongpfx'})
        with self.assertRaises(Exception) as ctx:
            index.handler(event, None)
        self.assertIn('Key not found', str(ctx.exception))

    @patch.object(index, 'dynamodb')
    def test_revoke_key_no_keys(self, mock_dynamo):
        """Reject revoke if user has no keys."""
        mock_table = MagicMock()
        mock_table.query.return_value = {'Items': []}
        mock_dynamo.Table.return_value = mock_table

        event = make_event('revokeMCPApiKey', username='alice',
                           arguments={'keyPrefix': 'lma_anything'})
        with self.assertRaises(Exception) as ctx:
            index.handler(event, None)
        self.assertIn('Key not found', str(ctx.exception))


class TestListKeys(unittest.TestCase):
    """Test listing API keys."""

    @patch.object(index, 'dynamodb')
    def test_list_keys_with_results(self, mock_dynamo):
        """List returns formatted key info."""
        mock_table = MagicMock()
        mock_table.query.return_value = {'Items': [
            {'KeyPrefix': 'lma_abcdef12', 'CreatedAt': '2025-01-01T00:00:00Z', 'Enabled': 'true'},
        ]}
        mock_dynamo.Table.return_value = mock_table

        event = make_event('listMCPApiKeys', username='alice')
        result = index.handler(event, None)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['keyPrefix'], 'lma_abcdef12')
        self.assertEqual(result[0]['createdAt'], '2025-01-01T00:00:00Z')
        self.assertTrue(result[0]['enabled'])

    @patch.object(index, 'dynamodb')
    def test_list_keys_empty(self, mock_dynamo):
        """List returns empty array when user has no keys."""
        mock_table = MagicMock()
        mock_table.query.return_value = {'Items': []}
        mock_dynamo.Table.return_value = mock_table

        event = make_event('listMCPApiKeys', username='alice')
        result = index.handler(event, None)
        self.assertEqual(result, [])

    @patch.object(index, 'dynamodb')
    def test_list_keys_disabled(self, mock_dynamo):
        """Disabled keys show enabled=False."""
        mock_table = MagicMock()
        mock_table.query.return_value = {'Items': [
            {'KeyPrefix': 'lma_disabled1', 'CreatedAt': '2025-01-01', 'Enabled': 'false'},
        ]}
        mock_dynamo.Table.return_value = mock_table

        event = make_event('listMCPApiKeys', username='alice')
        result = index.handler(event, None)
        self.assertFalse(result[0]['enabled'])


if __name__ == '__main__':
    unittest.main()
