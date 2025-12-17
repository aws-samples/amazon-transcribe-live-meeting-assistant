/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React, { useState } from 'react';
import PropTypes from 'prop-types';
import {
  Alert,
  Box,
  Button,
  ColumnLayout,
  Container,
  FormField,
  Header,
  Input,
  Link,
  SpaceBetween,
  Textarea,
} from '@awsui/components-react';
import { API, graphqlOperation, Logger } from 'aws-amplify';
import AuthConfigModal from './AuthConfigModal';

const logger = new Logger('CustomServersTab');

/**
 * Custom Servers Tab - Add custom HTTP MCP server endpoints
 */
const CustomServersTab = ({ onInstall }) => {
  const [serverName, setServerName] = useState('');
  const [serverUrl, setServerUrl] = useState('');
  const [description, setDescription] = useState('');
  const [requiresAuth, setRequiresAuth] = useState(false);
  const [installing, setInstalling] = useState(false);
  const [installSuccess, setInstallSuccess] = useState(null);
  const [installError, setInstallError] = useState(null);
  const [showAuthModal, setShowAuthModal] = useState(false);
  const [validationErrors, setValidationErrors] = useState({});

  const validateForm = () => {
    const errors = {};

    if (!serverName.trim()) {
      errors.serverName = 'Server name is required';
    }

    if (!serverUrl.trim()) {
      errors.serverUrl = 'Server URL is required';
    } else {
      try {
        const url = new URL(serverUrl);
        if (!['http:', 'https:'].includes(url.protocol)) {
          errors.serverUrl = 'URL must use HTTP or HTTPS protocol';
        }
      } catch (e) {
        errors.serverUrl = 'Invalid URL format';
      }
    }

    setValidationErrors(errors);
    return Object.keys(errors).length === 0;
  };

  const handleInstall = async (authConfig = null) => {
    if (!validateForm()) {
      return;
    }

    // If requires auth and no config provided, show auth modal
    if (requiresAuth && !authConfig) {
      setShowAuthModal(true);
      return;
    }

    setInstalling(true);
    setInstallError(null);
    setInstallSuccess(null);

    try {
      // Generate a unique server ID from the name
      const serverId = `custom/${serverName.toLowerCase().replace(/[^a-z0-9-]/g, '-')}`;

      logger.info('Installing custom MCP server:', serverId);

      const mutation = `
        mutation InstallMCPServer($input: InstallMCPServerInput!) {
          installMCPServer(input: $input) {
            ServerId
            Success
            Message
            BuildId
          }
        }
      `;

      const result = await API.graphql(
        graphqlOperation(mutation, {
          input: {
            ServerId: serverId,
            Name: serverName,
            NpmPackage: serverUrl, // For HTTP servers, this is the URL
            PackageType: 'streamable-http',
            Version: '1.0.0', // Custom servers don't have versions
            Transport: ['streamable-http'],
            RequiresAuth: requiresAuth,
            ServerUrl: serverUrl,
            AuthConfig: authConfig ? JSON.stringify(authConfig) : null,
          },
        }),
      );

      const response = result.data.installMCPServer;

      if (response.Success) {
        setInstallSuccess(`${serverName} activated successfully! The server is ready to use.`);
        logger.info('Custom server installation successful:', response);

        // Clear form
        setServerName('');
        setServerUrl('');
        setDescription('');
        setRequiresAuth(false);

        // Call parent callback if provided
        if (onInstall) {
          onInstall({ id: serverId, name: serverName });
        }
      } else {
        setInstallError(response.Message || 'Installation failed');
        logger.error('Installation failed:', response.Message);
      }
    } catch (err) {
      logger.error('Error installing custom server:', err);
      setInstallError(err.message || 'Failed to install custom server');
    } finally {
      setInstalling(false);
    }
  };

  const handleAuthSubmit = async (authConfig) => {
    await handleInstall(authConfig);
  };

  return (
    <>
      <AuthConfigModal
        visible={showAuthModal}
        onDismiss={() => setShowAuthModal(false)}
        onSubmit={handleAuthSubmit}
        server={{
          name: serverName,
          requiresAuth: true,
          packageType: 'streamable-http',
          transport: ['streamable-http'],
        }}
      />

      <Container
        header={
          <Header variant="h3" description="Add your own MCP server endpoints via HTTP">
            Custom MCP Servers
          </Header>
        }
      >
        <SpaceBetween size="l">
          {installSuccess && (
            <Alert type="success" dismissible onDismiss={() => setInstallSuccess(null)} header="Server Activated">
              {installSuccess}
            </Alert>
          )}

          {installError && (
            <Alert type="error" dismissible onDismiss={() => setInstallError(null)} header="Installation Failed">
              {installError}
            </Alert>
          )}

          <Alert type="info">
            Connect to your own MCP server endpoints via HTTP. These servers activate instantly without requiring a
            build. Perfect for internal company tools or custom integrations.
          </Alert>

          <SpaceBetween size="m">
            <Box variant="h4">Add Custom HTTP Server</Box>

            <form
              onSubmit={(e) => {
                e.preventDefault();
                handleInstall();
              }}
            >
              <SpaceBetween size="m">
                <ColumnLayout columns={2}>
                  <FormField
                    label="Server Name"
                    description="A friendly name for your server"
                    errorText={validationErrors.serverName}
                  >
                    <Input
                      value={serverName}
                      onChange={({ detail }) => {
                        setServerName(detail.value);
                        setValidationErrors((prev) => ({ ...prev, serverName: null }));
                      }}
                      placeholder="My Custom Server"
                      disabled={installing}
                    />
                  </FormField>

                  <FormField
                    label="Server URL"
                    description="HTTP/HTTPS endpoint for your MCP server"
                    errorText={validationErrors.serverUrl}
                  >
                    <Input
                      value={serverUrl}
                      onChange={({ detail }) => {
                        setServerUrl(detail.value);
                        setValidationErrors((prev) => ({ ...prev, serverUrl: null }));
                      }}
                      placeholder="https://your-server.com/mcp"
                      disabled={installing}
                      type="url"
                    />
                  </FormField>
                </ColumnLayout>

                <FormField label="Description (Optional)" description="What does this server do?">
                  <Textarea
                    value={description}
                    onChange={({ detail }) => setDescription(detail.value)}
                    placeholder="This server provides access to..."
                    disabled={installing}
                    rows={3}
                  />
                </FormField>

                <FormField
                  label="Authentication"
                  description="Does this server require authentication credentials?"
                  stretch
                >
                  <Box>
                    <label
                      htmlFor="requires-auth-checkbox"
                      style={{ display: 'flex', alignItems: 'center', cursor: 'pointer' }}
                    >
                      <input
                        id="requires-auth-checkbox"
                        type="checkbox"
                        checked={requiresAuth}
                        onChange={(e) => setRequiresAuth(e.target.checked)}
                        disabled={installing}
                        style={{ marginRight: '8px' }}
                      />
                      <span>This server requires authentication (API key, token, etc.)</span>
                    </label>
                  </Box>
                </FormField>

                <Box float="right">
                  <SpaceBetween direction="horizontal" size="xs">
                    <Button
                      onClick={() => {
                        setServerName('');
                        setServerUrl('');
                        setDescription('');
                        setRequiresAuth(false);
                        setValidationErrors({});
                      }}
                      disabled={installing}
                    >
                      Clear
                    </Button>
                    <Button
                      variant="primary"
                      onClick={() => handleInstall()}
                      loading={installing}
                      disabled={installing}
                    >
                      {installing ? 'Adding Server...' : 'Add Server'}
                    </Button>
                  </SpaceBetween>
                </Box>
              </SpaceBetween>
            </form>
          </SpaceBetween>

          <Box padding={{ top: 'l' }}>
            <Box variant="h4" margin={{ bottom: 's' }}>
              Requirements for Custom HTTP Servers
            </Box>
            <SpaceBetween size="s">
              <Box>
                <strong>1. MCP Protocol Support</strong>
                <Box fontSize="body-s" color="text-body-secondary" margin={{ top: 'xs' }}>
                  Your server must implement the{' '}
                  <Link external href="https://modelcontextprotocol.io">
                    Model Context Protocol
                  </Link>{' '}
                  specification with HTTP transport (streamable-http).
                </Box>
              </Box>

              <Box>
                <strong>2. HTTPS Recommended</strong>
                <Box fontSize="body-s" color="text-body-secondary" margin={{ top: 'xs' }}>
                  Use HTTPS for production servers to ensure secure communication. HTTP is allowed for local development
                  only.
                </Box>
              </Box>

              <Box>
                <strong>3. Authentication (Optional)</strong>
                <Box fontSize="body-s" color="text-body-secondary" margin={{ top: 'xs' }}>
                  If your server requires authentication, check the authentication box and you&apos;ll be prompted to
                  provide credentials (API keys, tokens, etc.).
                </Box>
              </Box>

              <Box>
                <strong>4. Instant Activation</strong>
                <Box fontSize="body-s" color="text-body-secondary" margin={{ top: 'xs' }}>
                  HTTP servers activate instantly without requiring a build process. They&apos;re available immediately
                  after adding.
                </Box>
              </Box>
            </SpaceBetween>
          </Box>
        </SpaceBetween>
      </Container>
    </>
  );
};

CustomServersTab.propTypes = {
  onInstall: PropTypes.func,
};

CustomServersTab.defaultProps = {
  onInstall: () => {},
};

export default CustomServersTab;
