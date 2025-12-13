/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React, { useCallback, useEffect, useState } from 'react';
import PropTypes from 'prop-types';
import {
  Alert,
  Badge,
  Box,
  Button,
  ColumnLayout,
  Container,
  ExpandableSection,
  Header,
  Modal,
  SpaceBetween,
  Spinner,
  StatusIndicator,
  Tabs,
} from '@awsui/components-react';
import { API, graphqlOperation, Logger } from 'aws-amplify';
import PublicRegistryTab from './PublicRegistryTab';
import './mcp-servers.css';

const logger = new Logger('MCPServersModal');

/**
 * MCP Servers Modal - Manage Model Context Protocol servers
 * Shows Lambda MCP servers (always available) and VP MCP (active meetings only)
 */
const MCPServersModal = ({ visible, onDismiss, vpData }) => {
  const [installedServers, setInstalledServers] = useState([]);
  const [loadingInstalled, setLoadingInstalled] = useState(false);
  const [installedError, setInstalledError] = useState(null);
  const [uninstalling, setUninstalling] = useState({});
  const [updating, setUpdating] = useState({});
  const [registryVersions, setRegistryVersions] = useState({});

  const fetchRegistryVersions = useCallback(async (servers) => {
    // Fetch latest versions from registry for each installed server
    // Use the PACKAGE version from the entry marked as isLatest
    try {
      const versions = {};
      // Use Promise.all to fetch versions in parallel instead of loop
      await Promise.all(
        servers.map(async (server) => {
          try {
            const response = await fetch(
              `https://registry.modelcontextprotocol.io/v0.1/servers?search=${encodeURIComponent(
                server.ServerId,
              )}&version=latest`,
            );
            if (response.ok) {
              const data = await response.json();
              // Find the entry marked as isLatest (not just the first one)
              const registryServer = data.servers?.find(
                (s) =>
                  // eslint-disable-next-line no-underscore-dangle
                  s.server.name === server.ServerId && s._meta?.['io.modelcontextprotocol.registry/official']?.isLatest,
              );
              if (registryServer && registryServer.server.packages?.[0]) {
                // Use the package version (e.g., 0.4.4), NOT the server version (e.g., 0.6.0)
                const latestVersion = registryServer.server.packages[0].version;
                versions[server.ServerId] = latestVersion;
                logger.info(`Registry version for ${server.ServerId}: ${latestVersion}`);
              }
            }
          } catch (err) {
            logger.warn(`Could not fetch registry version for ${server.ServerId}:`, err);
          }
        }),
      );
      setRegistryVersions(versions);
    } catch (err) {
      logger.error('Error fetching registry versions:', err);
    }
  }, []);

  const fetchInstalledServers = useCallback(async () => {
    setLoadingInstalled(true);
    setInstalledError(null);

    try {
      logger.info('Fetching installed MCP servers...');

      const query = `
        query ListInstalledMCPServers {
          listInstalledMCPServers {
            AccountId
            ServerId
            Name
            NpmPackage
            PackageType
            Version
            Status
            InstalledAt
            UpdatedAt
            RequiresAuth
            Transport
          }
        }
      `;

      const result = await API.graphql(graphqlOperation(query));
      const servers = result.data.listInstalledMCPServers || [];

      setInstalledServers(servers);
      logger.info(`Loaded ${servers.length} installed servers`);

      // Fetch latest versions from registry for update detection
      fetchRegistryVersions(servers);
    } catch (err) {
      logger.error('Error fetching installed servers:', err);
      setInstalledError(err.message || 'Failed to load installed servers');
    } finally {
      setLoadingInstalled(false);
    }
  }, [fetchRegistryVersions]);

  // Fetch installed servers when modal opens
  useEffect(() => {
    if (visible) {
      fetchInstalledServers();
    }
  }, [visible, fetchInstalledServers]);

  const handleInstallServer = (server) => {
    logger.info('Install/uninstall/update server requested:', server.id);
    // Refresh installed servers list after operations
    setTimeout(() => fetchInstalledServers(), 3000); // Wait for build to start
  };

  const handleUninstall = async (serverId) => {
    setUninstalling((prev) => ({ ...prev, [serverId]: true }));

    try {
      logger.info('Uninstalling MCP server:', serverId);

      const mutation = `
        mutation UninstallMCPServer($serverId: String!) {
          uninstallMCPServer(serverId: $serverId) {
            ServerId
            Success
            Message
          }
        }
      `;

      const result = await API.graphql(
        graphqlOperation(mutation, {
          serverId,
        }),
      );

      const response = result.data.uninstallMCPServer;

      if (response.Success) {
        logger.info('Uninstallation successful:', response);
        // Refresh the list
        fetchInstalledServers();
      } else {
        logger.error('Uninstallation failed:', response.Message);
        setInstalledError(response.Message || 'Uninstallation failed');
      }
    } catch (err) {
      logger.error('Error uninstalling server:', err);
      setInstalledError(err.message || 'Failed to uninstall server');
    } finally {
      setUninstalling((prev) => ({ ...prev, [serverId]: false }));
    }
  };

  const handleUpdate = async (serverId) => {
    setUpdating((prev) => ({ ...prev, [serverId]: true }));
    setInstalledError(null);

    try {
      logger.info('Updating MCP server:', serverId, 'to latest version');

      const mutation = `
        mutation UpdateMCPServer($input: UpdateMCPServerInput!) {
          updateMCPServer(input: $input) {
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
            Version: 'latest',
          },
        }),
      );

      const response = result.data.updateMCPServer;

      if (response.Success) {
        logger.info('Update successful:', response);
        // Refresh the list
        setTimeout(() => fetchInstalledServers(), 2000);
      } else {
        logger.error('Update failed:', response.Message);
        setInstalledError(response.Message || 'Update failed');
      }
    } catch (err) {
      logger.error('Error updating server:', err);
      setInstalledError(err.message || 'Failed to update server');
    } finally {
      setUpdating((prev) => ({ ...prev, [serverId]: false }));
    }
  };

  return (
    <Modal
      visible={visible}
      onDismiss={onDismiss}
      size="large"
      header="MCP Servers"
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button onClick={onDismiss}>Close</Button>
          </SpaceBetween>
        </Box>
      }
    >
      <Tabs
        tabs={[
          {
            label: 'Active Servers',
            id: 'active',
            content: (
              <SpaceBetween size="l">
                {/* VP MCP Server - Only During Active Meetings */}
                <Container
                  header={
                    <Header
                      variant="h3"
                      description="Chrome DevTools MCP for VP browser control (active meetings only)"
                    >
                      Virtual Participant Browser Control
                    </Header>
                  }
                >
                  <SpaceBetween size="m">
                    <ColumnLayout columns={3} variant="text-grid">
                      <Box>
                        <Box variant="awsui-key-label">Status</Box>
                        <StatusIndicator type={vpData?.status === 'JOINED' ? 'success' : 'stopped'}>
                          {vpData?.status === 'JOINED' ? 'Ready' : 'Not Available'}
                        </StatusIndicator>
                      </Box>

                      <Box>
                        <Box variant="awsui-key-label">Communication</Box>
                        <Box>AppSync Event API</Box>
                      </Box>

                      <Box>
                        <Box variant="awsui-key-label">Available Tools</Box>
                        <Box>{vpData?.status === 'JOINED' ? '24 tools' : '0 tools'}</Box>
                      </Box>
                    </ColumnLayout>

                    {vpData?.status === 'JOINED' ? (
                      <>
                        <Alert type="success" header="Ready for Agent Control">
                          The Strands agent can control the Virtual Participant&apos;s browser using natural language
                          commands. The agent will automatically open the VNC preview before controlling the browser.
                          <Box margin={{ top: 's' }}>
                            <strong>Try asking:</strong>
                            <ul style={{ marginTop: '8px', marginBottom: 0 }}>
                              <li>&quot;show me apple.com&quot;</li>
                              <li>&quot;open aws.amazon.com&quot;</li>
                              <li>&quot;take a screenshot&quot;</li>
                            </ul>
                          </Box>
                        </Alert>

                        <ExpandableSection header="Available Tools" variant="container">
                          <SpaceBetween size="m">
                            <Box>
                              <Box variant="h4">Browser Control (2 tools)</Box>
                              <ColumnLayout columns={2}>
                                <Box>
                                  <Box>
                                    <code style={{ fontSize: '13px', fontWeight: 'bold', color: '#0073bb' }}>
                                      open_url
                                    </code>
                                  </Box>
                                  <Box fontSize="body-s" color="text-body-secondary">
                                    Opens a URL in a new browser tab
                                  </Box>
                                </Box>

                                <Box>
                                  <Box>
                                    <code style={{ fontSize: '13px', fontWeight: 'bold', color: '#0073bb' }}>
                                      screenshot
                                    </code>
                                  </Box>
                                  <Box fontSize="body-s" color="text-body-secondary">
                                    Takes a screenshot of current page
                                  </Box>
                                </Box>
                              </ColumnLayout>
                            </Box>

                            <Box>
                              <Box variant="h4">Chrome DevTools (22 tools)</Box>
                              <Box fontSize="body-s" color="text-body-secondary">
                                Full Chrome DevTools Protocol access including navigation, input automation, performance
                                analysis, network debugging, and more. Available via MCP when VP is active.
                              </Box>
                            </Box>
                          </SpaceBetween>
                        </ExpandableSection>
                      </>
                    ) : (
                      <Alert type="info" header="MCP Server Not Available">
                        The MCP server will be available when a Virtual Participant joins this meeting. The VP must be
                        active and connected for browser control tools to work.
                      </Alert>
                    )}
                  </SpaceBetween>
                </Container>

                {/* Lambda MCP Servers - Always Available */}
                <Container
                  header={
                    <Header
                      variant="h3"
                      description="MCP servers running in the Strands agent (always available)"
                      actions={
                        <Button onClick={fetchInstalledServers} iconName="refresh" disabled={loadingInstalled}>
                          Refresh
                        </Button>
                      }
                    >
                      Agent MCP Servers
                    </Header>
                  }
                >
                  <SpaceBetween size="m">
                    <Alert type="success" header="Available for All Meetings">
                      These MCP servers run in the Strands agent Lambda function and are available for both active and
                      completed meetings. They don&apos;t require a Virtual Participant.
                    </Alert>

                    {loadingInstalled && (
                      <Box textAlign="center" padding="l">
                        <Spinner size="large" />
                        <Box margin={{ top: 's' }}>Loading installed servers...</Box>
                      </Box>
                    )}

                    {installedError && (
                      <Alert type="error" header="Error Loading Servers">
                        {installedError}
                      </Alert>
                    )}

                    {!loadingInstalled && !installedError && installedServers.length === 0 && (
                      <Box textAlign="center" padding="xxl" color="text-body-secondary">
                        No MCP servers installed yet. Browse the Public Registry to install servers.
                      </Box>
                    )}

                    {!loadingInstalled && !installedError && installedServers.length > 0 && (
                      <ColumnLayout columns={2}>
                        {installedServers.map((server) => {
                          const latestVersion = registryVersions[server.ServerId];
                          const hasUpdate = latestVersion && latestVersion !== server.Version;

                          return (
                            <Container key={server.ServerId}>
                              <SpaceBetween size="s">
                                <Box>
                                  <SpaceBetween direction="horizontal" size="xs">
                                    <Box variant="h4">{server.Name}</Box>
                                    <Badge color={server.PackageType === 'pypi' ? 'grey' : 'blue'}>
                                      {server.PackageType || 'pypi'}
                                    </Badge>
                                    {server.Status === 'ACTIVE' && <Badge color="green">Active</Badge>}
                                    {server.Status === 'BUILDING' && <Badge color="blue">Building</Badge>}
                                    {server.Status === 'UPDATING' && <Badge color="blue">Updating</Badge>}
                                    {server.Status === 'FAILED' && <Badge color="red">Failed</Badge>}
                                    {hasUpdate && <Badge color="blue">Update Available</Badge>}
                                    {server.RequiresAuth && <Badge color="blue">Requires Auth</Badge>}
                                  </SpaceBetween>
                                </Box>

                                <ColumnLayout columns={2} variant="text-grid">
                                  <Box>
                                    <Box variant="awsui-key-label">Package</Box>
                                    <Box>
                                      <code style={{ fontSize: '11px' }}>{server.NpmPackage}</code>
                                    </Box>
                                  </Box>

                                  <Box>
                                    <Box variant="awsui-key-label">Version</Box>
                                    <Box>
                                      {server.Version}
                                      {hasUpdate && (
                                        <Box fontSize="body-s" color="text-status-info">
                                          → {latestVersion} available
                                        </Box>
                                      )}
                                    </Box>
                                  </Box>

                                  <Box>
                                    <Box variant="awsui-key-label">Transport</Box>
                                    <Box>
                                      {Array.isArray(server.Transport) ? server.Transport.join(', ') : server.Transport}
                                    </Box>
                                  </Box>

                                  <Box>
                                    <Box variant="awsui-key-label">Installed</Box>
                                    <Box>{new Date(server.InstalledAt).toLocaleDateString()}</Box>
                                  </Box>
                                </ColumnLayout>

                                <Box float="right">
                                  <SpaceBetween direction="horizontal" size="xs">
                                    {hasUpdate && (
                                      <Button
                                        variant="primary"
                                        onClick={() => handleUpdate(server.ServerId)}
                                        loading={updating[server.ServerId]}
                                        disabled={updating[server.ServerId] || uninstalling[server.ServerId]}
                                      >
                                        {updating[server.ServerId] ? 'Updating...' : 'Update'}
                                      </Button>
                                    )}
                                    <Button
                                      onClick={() => handleUninstall(server.ServerId)}
                                      loading={uninstalling[server.ServerId]}
                                      disabled={uninstalling[server.ServerId] || updating[server.ServerId]}
                                    >
                                      {uninstalling[server.ServerId] ? 'Uninstalling...' : 'Uninstall'}
                                    </Button>
                                  </SpaceBetween>
                                </Box>
                              </SpaceBetween>
                            </Container>
                          );
                        })}
                      </ColumnLayout>
                    )}

                    <Alert type="info">Maximum 5 MCP servers can be enabled at once to maintain performance.</Alert>
                  </SpaceBetween>
                </Container>
              </SpaceBetween>
            ),
          },
          {
            label: 'Public Registry',
            id: 'registry',
            content: <PublicRegistryTab onInstall={handleInstallServer} installedServers={installedServers} />,
          },
          {
            label: 'Custom Servers',
            id: 'custom',
            content: (
              <Container
                header={
                  <Header variant="h3" description="Add your own MCP server endpoints">
                    Custom MCP Servers
                  </Header>
                }
              >
                <SpaceBetween size="m">
                  <Alert type="info">
                    Connect to your own MCP server endpoints via HTTP or WebSocket. Useful for internal company tools or
                    custom integrations.
                  </Alert>

                  <Box textAlign="center" padding={{ vertical: 'xxl' }}>
                    <Box variant="h3" color="text-body-secondary">
                      Custom Servers
                    </Box>
                    <Box color="text-body-secondary" margin={{ top: 's' }}>
                      Connect to your own MCP server endpoints
                    </Box>
                    <Box margin={{ top: 'm' }}>
                      <StatusIndicator type="info">Feature in development</StatusIndicator>
                    </Box>
                  </Box>
                </SpaceBetween>
              </Container>
            ),
          },
        ]}
      />
    </Modal>
  );
};

MCPServersModal.propTypes = {
  visible: PropTypes.bool.isRequired,
  onDismiss: PropTypes.func.isRequired,
  vpData: PropTypes.shape({
    CallId: PropTypes.string,
    mcpReady: PropTypes.bool,
    vncReady: PropTypes.bool,
    status: PropTypes.string,
  }),
};

MCPServersModal.defaultProps = {
  vpData: null,
};

export default MCPServersModal;
