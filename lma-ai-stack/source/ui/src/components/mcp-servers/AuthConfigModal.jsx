/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React, { useState } from 'react';
import PropTypes from 'prop-types';
import { Alert, Box, Button, FormField, Input, Modal, SpaceBetween } from '@awsui/components-react';

/**
 * Authentication Configuration Modal
 * Collects credentials for MCP servers that require authentication
 */
const AuthConfigModal = ({ visible, onDismiss, onSubmit, server }) => {
  const [apiKey, setApiKey] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  const handleSubmit = async () => {
    if (!apiKey.trim()) {
      setError('API key is required');
      return;
    }

    setLoading(true);
    setError(null);

    try {
      // Build auth config based on server type
      const authConfig = {};

      // For Smithery servers (like Salesforce MCP)
      if (server.serverUrl?.includes('smithery.ai')) {
        authConfig.smithery_api_key = apiKey.trim();
      } else {
        // Generic API key
        authConfig.api_key = apiKey.trim();
      }

      await onSubmit(authConfig);

      // Reset form
      setApiKey('');
      onDismiss();
    } catch (err) {
      setError(err.message || 'Failed to save credentials');
    } finally {
      setLoading(false);
    }
  };

  const handleCancel = () => {
    setApiKey('');
    setError(null);
    onDismiss();
  };

  return (
    <Modal
      visible={visible}
      onDismiss={handleCancel}
      header={`Authentication Required: ${server?.name || 'MCP Server'}`}
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button onClick={handleCancel} disabled={loading}>
              Cancel
            </Button>
            <Button variant="primary" onClick={handleSubmit} loading={loading}>
              Install with Credentials
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      <SpaceBetween size="m">
        <Alert type="info" header="Credentials Required">
          This MCP server requires authentication to access its tools. Your credentials will be encrypted and stored
          securely.
        </Alert>

        {error && (
          <Alert type="error" dismissible onDismiss={() => setError(null)}>
            {error}
          </Alert>
        )}

        <FormField
          label="API Key"
          description={
            server?.serverUrl?.includes('smithery.ai')
              ? 'Enter your Smithery API key. Get one at https://smithery.ai'
              : 'Enter the API key or access token for this MCP server'
          }
        >
          <Input
            value={apiKey}
            onChange={({ detail }) => setApiKey(detail.value)}
            placeholder={server?.serverUrl?.includes('smithery.ai') ? 'sk-smithery-...' : 'Enter API key'}
            type="password"
            disabled={loading}
          />
        </FormField>

        {server?.homepage && (
          <Box fontSize="body-s" color="text-body-secondary">
            Need help? Check the{' '}
            <a href={server.homepage} target="_blank" rel="noopener noreferrer">
              documentation
            </a>{' '}
            for instructions on obtaining credentials.
          </Box>
        )}
      </SpaceBetween>
    </Modal>
  );
};

AuthConfigModal.propTypes = {
  visible: PropTypes.bool.isRequired,
  onDismiss: PropTypes.func.isRequired,
  onSubmit: PropTypes.func.isRequired,
  server: PropTypes.shape({
    id: PropTypes.string,
    name: PropTypes.string,
    serverUrl: PropTypes.string,
    homepage: PropTypes.string,
  }),
};

AuthConfigModal.defaultProps = {
  server: null,
};

export default AuthConfigModal;
