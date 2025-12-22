/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React, { useState, useEffect } from 'react';
import PropTypes from 'prop-types';
import { API } from 'aws-amplify';
import { Alert, Box, Button, FormField, Input, Modal, Select, SpaceBetween, Textarea } from '@awsui/components-react';
import { initOAuthFlow } from '../../graphql/mutations';

/**
 * Generic Authentication Configuration Modal
 * Supports multiple auth types: Bearer Token, Custom Headers, OAuth 2.1
 */
const AuthConfigModal = ({ visible, onDismiss, onSubmit, server }) => {
  const [authType, setAuthType] = useState({ value: 'bearer', label: 'Bearer Token' });
  const [bearerToken, setBearerToken] = useState('');
  const [customHeaders, setCustomHeaders] = useState('{\n  "X-API-Key": "your-key-here"\n}');
  const [envVars, setEnvVars] = useState('{\n  "API_KEY": "your-key-here"\n}');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  // OAuth state
  const [oauthProvider, setOauthProvider] = useState('salesforce');
  const [oauthClientId, setOauthClientId] = useState('');
  const [oauthScopes, setOauthScopes] = useState('');
  const [oauthAuthUrl, setOauthAuthUrl] = useState('');
  const [oauthTokenUrl, setOauthTokenUrl] = useState('');
  const [isAuthorizingOAuth, setIsAuthorizingOAuth] = useState(false);

  // Determine if this is an HTTP or PyPI server based on transport
  const isHttpServer = server?.transport?.includes('streamable-http') || server?.packageType === 'streamable-http';

  // PKCE utilities - defined before use
  const base64UrlEncode = (buffer) => {
    return btoa(String.fromCharCode(...buffer))
      .replace(/\+/g, '-')
      .replace(/\//g, '_')
      .replace(/=/g, '');
  };

  const generateCodeVerifier = () => {
    const array = new Uint8Array(32);
    crypto.getRandomValues(array);
    return base64UrlEncode(array);
  };

  const generateCodeChallenge = async (verifier) => {
    const encoder = new TextEncoder();
    const data = encoder.encode(verifier);
    const digest = await crypto.subtle.digest('SHA-256', data);
    return base64UrlEncode(new Uint8Array(digest));
  };

  // OAuth provider presets
  const OAUTH_PROVIDERS = {
    salesforce: {
      name: 'Salesforce',
      authorizationUrl: 'https://login.salesforce.com/services/oauth2/authorize',
      tokenUrl: 'https://login.salesforce.com/services/oauth2/token',
      defaultScopes: 'api refresh_token offline_access',
      instructions:
        'Create a Connected App in Salesforce Setup with OAuth enabled. ' +
        'Use the OAuth Callback URL from your stack outputs.',
    },
    google: {
      name: 'Google',
      authorizationUrl: 'https://accounts.google.com/o/oauth2/v2/auth',
      tokenUrl: 'https://oauth2.googleapis.com/token',
      defaultScopes: 'openid profile email',
      instructions: 'Create OAuth 2.0 credentials in Google Cloud Console.',
    },
    microsoft: {
      name: 'Microsoft',
      authorizationUrl: 'https://login.microsoftonline.com/common/oauth2/v2.0/authorize',
      tokenUrl: 'https://login.microsoftonline.com/common/oauth2/v2.0/token',
      defaultScopes: 'openid profile email',
      instructions: 'Register an application in Azure AD.',
    },
    custom: {
      name: 'Custom OAuth Provider',
      authorizationUrl: '',
      tokenUrl: '',
      defaultScopes: '',
      instructions: 'Enter your OAuth provider details below.',
    },
  };

  const authTypeOptions = isHttpServer
    ? [
        { value: 'bearer', label: 'Bearer Token' },
        { value: 'custom_headers', label: 'Custom Headers (JSON)' },
        { value: 'oauth2', label: 'OAuth 2.1 with PKCE' },
      ]
    : [
        { value: 'bearer', label: 'Bearer Token' },
        { value: 'env_vars', label: 'Environment Variables (JSON)' },
        { value: 'oauth2', label: 'OAuth 2.1 with PKCE (HTTP servers only)', disabled: true },
      ];

  // Update OAuth URLs when provider changes
  useEffect(() => {
    if (oauthProvider && OAUTH_PROVIDERS[oauthProvider]) {
      const provider = OAUTH_PROVIDERS[oauthProvider];
      setOauthAuthUrl(provider.authorizationUrl);
      setOauthTokenUrl(provider.tokenUrl);
      setOauthScopes(provider.defaultScopes);
    }
  }, [oauthProvider]);

  // OAuth authorization handler
  const handleOAuthAuthorize = async () => {
    setIsAuthorizingOAuth(true);
    setError(null);

    try {
      // Validate inputs
      if (!oauthClientId.trim()) {
        throw new Error('Client ID is required');
      }
      if (!oauthAuthUrl.trim() || !oauthTokenUrl.trim()) {
        throw new Error('Authorization and Token URLs are required');
      }

      // Generate PKCE parameters
      const codeVerifier = generateCodeVerifier();
      const codeChallenge = await generateCodeChallenge(codeVerifier);

      // Store for callback
      sessionStorage.setItem('oauth_code_verifier', codeVerifier);
      sessionStorage.setItem('oauth_server_id', server.id);

      // Initialize OAuth flow
      const response = await API.graphql({
        query: initOAuthFlow,
        variables: {
          input: {
            serverId: server.id,
            provider: oauthProvider,
            clientId: oauthClientId.trim(),
            authorizationUrl: oauthAuthUrl.trim(),
            tokenUrl: oauthTokenUrl.trim(),
            scopes: oauthScopes.split(' ').filter((s) => s.trim()),
            codeChallenge,
          },
        },
      });

      const result = response.data.initOAuthFlow;

      if (!result.success && result.error) {
        throw new Error(result.error);
      }

      const { authorizationUrl, state } = result;

      // Store state for verification
      sessionStorage.setItem('oauth_state', state);

      // Open OAuth authorization in popup
      const width = 600;
      const height = 700;
      const left = window.screenX + (window.outerWidth - width) / 2;
      const top = window.screenY + (window.outerHeight - height) / 2;

      const popup = window.open(
        authorizationUrl,
        'OAuth Authorization',
        `width=${width},height=${height},left=${left},top=${top},resizable=yes,scrollbars=yes`,
      );

      if (!popup) {
        throw new Error('Popup blocked. Please allow popups for this site.');
      }

      // Listen for callback message from popup
      const messageHandler = (event) => {
        if (event.origin !== window.location.origin) return;

        if (event.data.type === 'oauth_success') {
          window.removeEventListener('message', messageHandler);
          setIsAuthorizingOAuth(false);
          onDismiss();
          // Parent component will handle success notification
        } else if (event.data.type === 'oauth_error') {
          window.removeEventListener('message', messageHandler);
          setError(`Authorization failed: ${event.data.error}`);
          setIsAuthorizingOAuth(false);
        }
      };

      window.addEventListener('message', messageHandler);

      // Check if popup was closed
      const checkPopup = setInterval(() => {
        if (popup.closed) {
          clearInterval(checkPopup);
          window.removeEventListener('message', messageHandler);
          if (isAuthorizingOAuth) {
            setIsAuthorizingOAuth(false);
            setError('Authorization cancelled');
          }
        }
      }, 1000);
    } catch (err) {
      setError(`OAuth initialization failed: ${err.message}`);
      setIsAuthorizingOAuth(false);
    }
  };

  const validateCustomHeaders = () => {
    const jsonToValidate =
      authType.value === 'custom_headers' || authType.value === 'env_vars' ? customHeaders : envVars;
    try {
      const parsed = JSON.parse(jsonToValidate);
      if (typeof parsed !== 'object' || Array.isArray(parsed)) {
        return authType.value === 'custom_headers'
          ? 'Headers must be a JSON object'
          : 'Environment variables must be a JSON object';
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
    } else if (authType.value === 'custom_headers' || authType.value === 'env_vars') {
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
        // HTTP servers use headers
        authConfig.headers = JSON.parse(customHeaders);
      } else if (authType.value === 'env_vars') {
        // PyPI servers use environment variables
        authConfig.env = JSON.parse(customHeaders);
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
            <Button onClick={handleCancel} disabled={loading || isAuthorizingOAuth}>
              Cancel
            </Button>
            {authType.value !== 'oauth2' && (
              <Button variant="primary" onClick={handleSubmit} loading={loading} disabled={isAuthorizingOAuth}>
                Install with Credentials
              </Button>
            )}
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

        {authType.value === 'custom_headers' && (
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

        {authType.value === 'env_vars' && (
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
          <>
            <Alert type="info" header="OAuth 2.1 with PKCE">
              OAuth 2.1 provides secure authentication with automatic token refresh. Tokens are encrypted and stored
              securely. You&apos;ll be redirected to authorize access in a popup window.
            </Alert>

            <FormField label="OAuth Provider" description="Select your OAuth provider or choose Custom">
              <Select
                selectedOption={{ value: oauthProvider, label: OAUTH_PROVIDERS[oauthProvider].name }}
                onChange={({ detail }) => setOauthProvider(detail.selectedOption.value)}
                options={Object.entries(OAUTH_PROVIDERS).map(([key, val]) => ({
                  value: key,
                  label: val.name,
                }))}
                disabled={loading || isAuthorizingOAuth}
              />
            </FormField>

            <Alert type="info">{OAUTH_PROVIDERS[oauthProvider].instructions}</Alert>

            <FormField label="Client ID" description="Your OAuth application's client ID (public identifier)">
              <Input
                value={oauthClientId}
                onChange={({ detail }) => setOauthClientId(detail.value)}
                placeholder="Enter client ID"
                disabled={loading || isAuthorizingOAuth}
              />
            </FormField>

            <FormField label="Scopes" description="Space-separated list of OAuth scopes (e.g., 'api refresh_token')">
              <Input
                value={oauthScopes}
                onChange={({ detail }) => setOauthScopes(detail.value)}
                placeholder="api refresh_token"
                disabled={loading || isAuthorizingOAuth}
              />
            </FormField>

            {oauthProvider === 'custom' && (
              <>
                <FormField label="Authorization URL" description="OAuth authorization endpoint">
                  <Input
                    value={oauthAuthUrl}
                    onChange={({ detail }) => setOauthAuthUrl(detail.value)}
                    placeholder="https://provider.com/oauth/authorize"
                    disabled={loading || isAuthorizingOAuth}
                  />
                </FormField>

                <FormField label="Token URL" description="OAuth token endpoint">
                  <Input
                    value={oauthTokenUrl}
                    onChange={({ detail }) => setOauthTokenUrl(detail.value)}
                    placeholder="https://provider.com/oauth/token"
                    disabled={loading || isAuthorizingOAuth}
                  />
                </FormField>
              </>
            )}

            <Box>
              <Button variant="primary" onClick={handleOAuthAuthorize} loading={isAuthorizingOAuth} disabled={loading}>
                {isAuthorizingOAuth ? 'Authorizing...' : 'Authorize with OAuth'}
              </Button>
            </Box>

            <Alert type="warning" header="Important">
              <SpaceBetween size="xs">
                <Box>• Make sure popups are enabled for this site</Box>
                <Box>• You&apos;ll be redirected to the OAuth provider to grant access</Box>
                <Box>• Tokens will be automatically refreshed before expiration</Box>
              </SpaceBetween>
            </Alert>
          </>
        )}

        {authType.value !== 'oauth2' && (
          <Alert type="warning" header="Authentication Type Guide">
            <SpaceBetween size="xs">
              <Box>
                <strong>Bearer Token:</strong> For simple API key authentication.{' '}
                {isHttpServer ? 'Sent as Authorization header.' : 'Passed as MCP_API_KEY environment variable.'}
              </Box>
              <Box>
                <strong>Custom Headers:</strong> For HTTP servers requiring multiple HTTP headers. Provide as JSON
                object.
              </Box>
              <Box>
                <strong>Environment Variables:</strong> For PyPI servers requiring multiple environment variables.
                Provide as JSON object.
              </Box>
              <Box>
                <strong>OAuth 2.1:</strong> For services requiring OAuth flow with automatic token refresh (HTTP servers
                only).
              </Box>
            </SpaceBetween>
          </Alert>
        )}

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
