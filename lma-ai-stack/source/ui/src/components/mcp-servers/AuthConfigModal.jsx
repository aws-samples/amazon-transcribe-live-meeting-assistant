/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React, { useState } from 'react';
import PropTypes from 'prop-types';
import { Alert, Box, Button, FormField, Input, Modal, Select, SpaceBetween, Textarea } from '@awsui/components-react';

/**
 * Generic Authentication Configuration Modal
 * Supports multiple auth types: Bearer Token, Custom Headers, OAuth 2.1 (future)
 */
const AuthConfigModal = ({ visible, onDismiss, onSubmit, server }) => {
  const [authType, setAuthType] = useState({ value: 'bearer', label: 'Bearer Token' });
  const [bearerToken, setBearerToken] = useState('');
  const [customHeaders, setCustomHeaders] = useState('{\n  "X-API-Key": "your-key-here"\n}');
  const [envVars, setEnvVars] = useState('{\n  "API_KEY": "your-key-here"\n}');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  // Determine if this is an HTTP or PyPI server based on transport
  const isHttpServer = server?.transport?.includes('streamable-http') || server?.packageType === 'streamable-http';

  const authTypeOptions = [
    { value: 'bearer', label: 'Bearer Token' },
    {
      value: 'custom_headers',
      label: isHttpServer ? 'Custom Headers (JSON)' : 'Environment Variables (JSON)',
    },
    { value: 'oauth2', label: 'OAuth 2.1 (Coming Soon)', disabled: true },
  ];

  const validateCustomHeaders = () => {
    const jsonToValidate = authType.value === 'custom_headers' ? customHeaders : envVars;
    try {
      const parsed = JSON.parse(jsonToValidate);
      if (typeof parsed !== 'object' || Array.isArray(parsed)) {
        return isHttpServer ? 'Headers must be a JSON object' : 'Environment variables must be a JSON object';
      }
      return null;
    } catch (e) {
      return `Invalid JSON: ${e.message}`;
    }
  };

  const handleSubmit = async () => {
    setError(null);

    // Validate based on auth type
    if (authType.value === 'bearer') {
      if (!bearerToken.trim()) {
        setError('Bearer token is required');
        return;
      }
    } else if (authType.value === 'custom_headers') {
      const validationError = validateCustomHeaders();
      if (validationError) {
        setError(validationError);
        return;
      }
    }

    setLoading(true);

    try {
      // Build generic auth config
      const authConfig = {
        authType: authType.value,
      };

      if (authType.value === 'bearer') {
        authConfig.token = bearerToken.trim();
      } else if (authType.value === 'custom_headers') {
        if (isHttpServer) {
          // HTTP servers use headers
          authConfig.headers = JSON.parse(customHeaders);
        } else {
          // PyPI servers use environment variables
          authConfig.env = JSON.parse(customHeaders);
        }
      }

      await onSubmit(authConfig);

      // Reset form
      setBearerToken('');
      setCustomHeaders('{\n  "X-API-Key": "your-key-here"\n}');
      setEnvVars('{\n  "API_KEY": "your-key-here"\n}');
      onDismiss();
    } catch (err) {
      setError(err.message || 'Failed to save credentials');
    } finally {
      setLoading(false);
    }
  };

  const handleCancel = () => {
    setBearerToken('');
    setCustomHeaders('{\n  "X-API-Key": "your-key-here"\n}');
    setEnvVars('{\n  "API_KEY": "your-key-here"\n}');
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
          securely in DynamoDB.
        </Alert>

        {error && (
          <Alert type="error" dismissible onDismiss={() => setError(null)}>
            {error}
          </Alert>
        )}

        <FormField label="Authentication Type" description="Choose how to authenticate with this MCP server">
          <Select
            selectedOption={authType}
            onChange={({ detail }) => setAuthType(detail.selectedOption)}
            options={authTypeOptions}
            disabled={loading}
          />
        </FormField>

        {authType.value === 'bearer' && (
          <FormField
            label="Bearer Token"
            description="Enter your API token or access token. Will be sent as: Authorization: Bearer <token>"
          >
            <Input
              value={bearerToken}
              onChange={({ detail }) => setBearerToken(detail.value)}
              placeholder="sk-abc123... or your-api-token"
              type="password"
              disabled={loading}
            />
          </FormField>
        )}

        {authType.value === 'custom_headers' && isHttpServer && (
          <FormField
            label="Custom Headers (JSON)"
            description="Provide a JSON object with custom HTTP headers. These will be sent with every request."
            constraintText='Example: {"X-API-Key": "abc123"}'
          >
            <Textarea
              value={customHeaders}
              onChange={({ detail }) => setCustomHeaders(detail.value)}
              placeholder='{\n  "X-API-Key": "your-key-here",\n  "X-Custom-Header": "value"\n}'
              disabled={loading}
              rows={8}
            />
          </FormField>
        )}

        {authType.value === 'custom_headers' && !isHttpServer && (
          <FormField
            label="Environment Variables (JSON)"
            description="Provide a JSON object with environment variables. Passed to the MCP server process."
            constraintText='Example: {"CB_CONNECTION_STRING": "couchbases://..."}'
          >
            <Textarea
              value={customHeaders}
              onChange={({ detail }) => setCustomHeaders(detail.value)}
              placeholder='{\n  "API_KEY": "your-key-here",\n  "DATABASE_URL": "postgres://..."\n}'
              disabled={loading}
              rows={8}
            />
          </FormField>
        )}

        {authType.value === 'oauth2' && (
          <Alert type="info" header="OAuth 2.1 Support Coming Soon">
            OAuth 2.1 authentication flow will be available in a future update. For now, if your MCP server supports
            OAuth, you can:
            <ul style={{ marginTop: '8px' }}>
              <li>Obtain an access token manually from the provider</li>
              <li>Use Bearer Token authentication with the access token</li>
              <li>Or use Custom Headers to provide the token in a specific header format</li>
            </ul>
          </Alert>
        )}

        <Alert type="warning" header="Authentication Type Guide">
          <SpaceBetween size="xs">
            <Box>
              <strong>Bearer Token:</strong> For simple API key authentication.{' '}
              {isHttpServer ? 'Sent as Authorization header.' : 'Passed as MCP_API_KEY environment variable.'}
            </Box>
            <Box>
              <strong>{isHttpServer ? 'Custom Headers' : 'Environment Variables'}:</strong> For advanced authentication
              requiring multiple {isHttpServer ? 'HTTP headers' : 'environment variables'}. Provide as JSON object.
            </Box>
            <Box>
              <strong>OAuth 2.1:</strong> Coming soon - for services requiring OAuth flow with token refresh.
            </Box>
          </SpaceBetween>
        </Alert>

        <Box fontSize="body-s" color="text-body-secondary">
          <strong>Security Note:</strong> Credentials are encrypted using AWS KMS and stored in DynamoDB. They are only
          accessible by the Lambda function that connects to MCP servers.
        </Box>

        {server?.homepage && (
          <Box fontSize="body-s" color="text-body-secondary">
            Need help? Check the{' '}
            <a href={server.homepage} target="_blank" rel="noopener noreferrer">
              server documentation
            </a>{' '}
            for authentication instructions.
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
    transport: PropTypes.arrayOf(PropTypes.string),
    packageType: PropTypes.string,
  }),
};

AuthConfigModal.defaultProps = {
  server: null,
};

export default AuthConfigModal;
