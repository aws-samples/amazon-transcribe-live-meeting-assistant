/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import { ConsoleLogger } from 'aws-amplify/utils';
import { generateClient } from 'aws-amplify/api';
import React, { useEffect, useState, useCallback } from 'react';
import {
  Alert,
  Box,
  Button,
  Container,
  CopyToClipboard,
  Header,
  Modal,
  SpaceBetween,
  Spinner,
  StatusIndicator,
} from '@cloudscape-design/components';
import { generateMCPApiKey, revokeMCPApiKey, listMCPApiKeys } from '../../graphql/mutations';

const client = generateClient();
const logger = new ConsoleLogger('MCPApiKeySection');

const MCPApiKeySection = () => {
  const [keys, setKeys] = useState([]);
  const [loading, setLoading] = useState(true);
  const [generating, setGenerating] = useState(false);
  const [revoking, setRevoking] = useState(false);
  const [newKey, setNewKey] = useState(null);
  const [showRevokeConfirm, setShowRevokeConfirm] = useState(false);
  const [error, setError] = useState(null);

  const fetchKeys = useCallback(async () => {
    try {
      setLoading(true);
      const result = await client.graphql({ query: listMCPApiKeys });
      setKeys(result.data.listMCPApiKeys || []);
    } catch (err) {
      logger.error('Error fetching API keys:', err);
      setError('Failed to load API keys');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchKeys();
  }, [fetchKeys]);

  const handleGenerate = async () => {
    try {
      setGenerating(true);
      setError(null);
      const result = await client.graphql({ query: generateMCPApiKey });
      setNewKey(result.data.generateMCPApiKey);
      await fetchKeys();
    } catch (err) {
      const msg = err.errors?.[0]?.message || 'Failed to generate API key';
      logger.error('Error generating API key:', err);
      setError(msg);
    } finally {
      setGenerating(false);
    }
  };

  const handleRevoke = async (keyPrefix) => {
    try {
      setRevoking(true);
      setError(null);
      await client.graphql({ query: revokeMCPApiKey, variables: { keyPrefix } });
      setShowRevokeConfirm(false);
      await fetchKeys();
    } catch (err) {
      logger.error('Error revoking API key:', err);
      setError('Failed to revoke API key');
    } finally {
      setRevoking(false);
    }
  };

  const hasKey = keys.length > 0;

  const renderActions = () => (
    <Button onClick={handleGenerate} loading={generating} disabled={hasKey || loading}>
      Generate API Key
    </Button>
  );

  const renderKeyList = () =>
    keys.map((key) => (
      <Box key={key.keyPrefix}>
        <SpaceBetween direction="horizontal" size="s" alignItems="center">
          <StatusIndicator type={key.enabled ? 'success' : 'stopped'}>
            {key.keyPrefix}
            ••••••••
          </StatusIndicator>
          <Box color="text-body-secondary" fontSize="body-s">
            Created {new Date(key.createdAt).toLocaleDateString()}
          </Box>
          <Button variant="link" onClick={() => setShowRevokeConfirm(true)}>
            Revoke
          </Button>
        </SpaceBetween>
      </Box>
    ));

  const renderContent = () => {
    if (loading) {
      return <Spinner size="normal" />;
    }
    if (hasKey) {
      return <SpaceBetween size="s">{renderKeyList()}</SpaceBetween>;
    }
    return (
      <Box color="text-body-secondary">No API key generated. Click &quot;Generate API Key&quot; to create one.</Box>
    );
  };

  return (
    <>
      <Container
        header={
          <Header
            variant="h2"
            description="Generate a personal API key for MCP server access via x-api-key header."
            actions={renderActions()}
          >
            MCP API Key
          </Header>
        }
      >
        {error && (
          <Box margin={{ bottom: 's' }}>
            <Alert type="error" dismissible onDismiss={() => setError(null)}>
              {error}
            </Alert>
          </Box>
        )}
        {renderContent()}
      </Container>

      {newKey && (
        <Modal visible onDismiss={() => setNewKey(null)} header="API Key Generated">
          <SpaceBetween size="m">
            <Alert type="warning">Copy this key now. You won&apos;t be able to see it again.</Alert>
            <CopyToClipboard
              copyButtonAriaLabel="Copy API key"
              copySuccessText="API key copied"
              textToCopy={newKey.keyValue}
              variant="inline"
            />
          </SpaceBetween>
        </Modal>
      )}

      <Modal
        visible={showRevokeConfirm}
        onDismiss={() => setShowRevokeConfirm(false)}
        header="Revoke API Key"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={() => setShowRevokeConfirm(false)}>
                Cancel
              </Button>
              <Button variant="primary" loading={revoking} onClick={() => handleRevoke(keys[0]?.keyPrefix)}>
                Revoke
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        Are you sure you want to revoke your API key? Any MCP clients using this key will stop working.
      </Modal>
    </>
  );
};

export default MCPApiKeySection;
