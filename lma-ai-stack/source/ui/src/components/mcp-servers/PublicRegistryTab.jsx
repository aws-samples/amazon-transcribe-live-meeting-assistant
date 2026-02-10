/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React, { useEffect, useState } from 'react';
import PropTypes from 'prop-types';
import {
  Alert,
  Badge,
  Box,
  Button,
  ColumnLayout,
  Container,
  FormField,
  Header,
  Input,
  Link,
  SpaceBetween,
  Spinner,
} from '@awsui/components-react';
import { API, graphqlOperation, Logger } from 'aws-amplify';
import AuthConfigModal from './AuthConfigModal';

const logger = new Logger('PublicRegistryTab');

// Official MCP Registry API
const REGISTRY_API_BASE = 'https://registry.modelcontextprotocol.io/v0.1/servers';
const SERVERS_PER_PAGE = 50;

/**
 * Public Registry Tab - Browse and install MCP servers from official registry
 */
const PublicRegistryTab = ({ onInstall, installedServers = [] }) => {
  const [servers, setServers] = useState([]);
  const [loading, setLoading] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState(null);
  const [searchQuery, setSearchQuery] = useState('');
  const [nextCursor, setNextCursor] = useState(null);
  const [hasMore, setHasMore] = useState(false);
  const [installing, setInstalling] = useState({});
  const [uninstalling, setUninstalling] = useState({});
  const [updating, setUpdating] = useState({});
  const [installSuccess, setInstallSuccess] = useState(null);
  const [installError, setInstallError] = useState(null);
  const [showAuthModal, setShowAuthModal] = useState(false);
  const [pendingServer, setPendingServer] = useState(null);

  // Create maps for quick lookup
  const installedServerIds = new Set(installedServers.map((s) => s.ServerId));
  const installedServerMap = new Map(installedServers.map((s) => [s.ServerId, s]));

  const fetchMCPRegistry = async (searchTerm = '', cursor = null, append = false) => {
    if (append) {
      setLoadingMore(true);
    } else {
      setLoading(true);
      setServers([]);
    }
    setError(null);

    try {
      logger.info(`Fetching MCP registry${cursor ? ' (next page)' : ''}...`);

      // Build URL
      let url = `${REGISTRY_API_BASE}?limit=${SERVERS_PER_PAGE}&version=latest`;
      if (searchTerm) {
        url += `&search=${encodeURIComponent(searchTerm)}&version=latest`;
      }
      if (cursor) {
        url += `&cursor=${encodeURIComponent(cursor)}`;
      }

      const response = await fetch(url);

      if (!response.ok) {
        throw new Error(`Failed to fetch registry: ${response.statusText}`);
      }

      const data = await response.json();

      // Transform API response
      const serverList = (data.servers || [])
        .filter((item) => {
          // eslint-disable-next-line no-underscore-dangle
          const meta = item._meta?.['io.modelcontextprotocol.registry/official'];
          // Include latest versions with either packages OR remotes (HTTP servers)
          const hasPackages = item.server.packages && item.server.packages.length > 0;
          const hasRemotes = item.server.remotes && item.server.remotes.length > 0;
          return meta?.isLatest && (hasPackages || hasRemotes);
        })
        .map((item) => {
          const { server } = item;
          const name = server.name.split('/').pop() || server.name;

          const transports = [];
          if (server.packages && server.packages.length > 0) {
            transports.push('stdio');
          }
          if (server.remotes && server.remotes.length > 0) {
            server.remotes.forEach((remote) => transports.push(remote.type));
          }

          // Determine server type: package-based (pypi/npm) or remote (HTTP)
          let packageType = 'unknown';
          let packageIdentifier = '';
          let packageVersion = server.version;
          let isSupported = false;
          let serverUrl = null;

          if (server.packages && server.packages.length > 0) {
            // Package-based server (pypi, npm)
            const firstPackage = server.packages[0];
            packageType = firstPackage.registryType || 'unknown';
            packageIdentifier = firstPackage.identifier;
            packageVersion = firstPackage.version || server.version;
            isSupported = packageType === 'pypi'; // Only PyPI supported for now
          } else if (server.remotes && server.remotes.length > 0) {
            // Remote HTTP server
            const firstRemote = server.remotes[0];
            packageType = firstRemote.type; // e.g., 'streamable-http'
            serverUrl = firstRemote.url;
            packageIdentifier = serverUrl;
            isSupported = packageType === 'streamable-http'; // HTTP servers supported!
          }

          const requiresAuth =
            (server.remotes && server.remotes.some((r) => r.headers && r.headers.length > 0)) ||
            (server.packages &&
              server.packages.some((p) => p.environmentVariables && p.environmentVariables.some((v) => v.isSecret)));

          return {
            id: server.name,
            name: name.charAt(0).toUpperCase() + name.slice(1),
            description: server.description || 'No description available',
            category: 'Community',
            npmPackage: packageIdentifier, // Package name or HTTP URL
            packageType,
            isSupported, // PyPI or streamable-http
            transport: transports.length > 0 ? transports : ['stdio'],
            verified: true,
            requiresAuth,
            tools: [],
            homepage:
              server.repository?.url ||
              `https://registry.modelcontextprotocol.io/v0.1/servers/${encodeURIComponent(server.name)}/versions/${
                server.version
              }`,
            version: packageVersion,
            serverUrl, // For HTTP servers
          };
        });

      // Append or replace
      if (append) {
        setServers((prev) => [...prev, ...serverList]);
      } else {
        setServers(serverList);
      }

      // Update pagination
      setNextCursor(data.metadata?.nextCursor || null);
      setHasMore(!!data.metadata?.nextCursor);

      logger.info(
        `Loaded ${serverList.length} servers (total: ${
          append ? servers.length + serverList.length : serverList.length
        })`,
      );
    } catch (err) {
      logger.error('Error fetching MCP registry:', err);
      setError(err.message || 'Failed to load MCP registry');
    } finally {
      setLoading(false);
      setLoadingMore(false);
    }
  };

  const loadMore = () => {
    if (nextCursor && !loadingMore) {
      fetchMCPRegistry(searchQuery, nextCursor, true);
    }
  };

  const handleInstall = async (server, authConfig = null) => {
    // If server requires auth and no config provided, show auth modal
    if (server.requiresAuth && !authConfig) {
      setPendingServer(server);
      setShowAuthModal(true);
      return;
    }

    setInstalling((prev) => ({ ...prev, [server.id]: true }));
    setInstallError(null);
    setInstallSuccess(null);

    try {
      logger.info('Installing MCP server:', server.id);

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
            ServerId: server.id,
            Name: server.name,
            NpmPackage: server.npmPackage,
            PackageType: server.packageType,
            Version: server.version,
            Transport: server.transport,
            RequiresAuth: server.requiresAuth,
            ServerUrl: server.serverUrl, // For HTTP servers
            AuthConfig: authConfig ? JSON.stringify(authConfig) : null,
          },
        }),
      );

      const response = result.data.installMCPServer;

      if (response.Success) {
        const message =
          server.packageType === 'streamable-http'
            ? `${server.name} activated instantly!`
            : `${server.name} installation started. Build ID: ${response.BuildId || 'N/A'}`;
        setInstallSuccess(message);
        logger.info('Installation started:', response);

        // Call parent callback if provided
        if (onInstall) {
          onInstall(server);
        }
      } else {
        setInstallError(response.Message || 'Installation failed');
        logger.error('Installation failed:', response.Message);
      }
    } catch (err) {
      logger.error('Error installing server:', err);
      setInstallError(err.message || 'Failed to install server');
    } finally {
      setInstalling((prev) => ({ ...prev, [server.id]: false }));
    }
  };

  const handleAuthSubmit = async (authConfig) => {
    if (pendingServer) {
      await handleInstall(pendingServer, authConfig);
      setPendingServer(null);
    }
  };

  const handleUninstall = async (server) => {
    setUninstalling((prev) => ({ ...prev, [server.id]: true }));
    setInstallError(null);
    setInstallSuccess(null);

    try {
      logger.info('Uninstalling MCP server:', server.id);

      const mutation = `
        mutation UninstallMCPServer($serverId: ID!) {
          uninstallMCPServer(serverId: $serverId) {
            ServerId
            Success
            Message
          }
        }
      `;

      const result = await API.graphql(
        graphqlOperation(mutation, {
          serverId: server.id,
        }),
      );

      const response = result.data.uninstallMCPServer;

      if (response.Success) {
        setInstallSuccess(`${server.name} uninstalled successfully`);
        logger.info('Uninstallation successful:', response);

        // Call parent callback if provided
        if (onInstall) {
          onInstall(server);
        }
      } else {
        setInstallError(response.Message || 'Uninstallation failed');
        logger.error('Uninstallation failed:', response.Message);
      }
    } catch (err) {
      logger.error('Error uninstalling server:', err);
      setInstallError(err.message || 'Failed to uninstall server');
    } finally {
      setUninstalling((prev) => ({ ...prev, [server.id]: false }));
    }
  };

  const handleUpdate = async (server) => {
    setUpdating((prev) => ({ ...prev, [server.id]: true }));
    setInstallError(null);
    setInstallSuccess(null);

    try {
      logger.info('Updating MCP server:', server.id, 'to version:', server.version);

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
            ServerId: server.id,
            Version: server.version,
          },
        }),
      );

      const response = result.data.updateMCPServer;

      if (response.Success) {
        setInstallSuccess(
          `${server.name} update to v${server.version} started. Build ID: ${response.BuildId || 'N/A'}`,
        );
        logger.info('Update started:', response);

        // Call parent callback if provided
        if (onInstall) {
          onInstall(server);
        }
      } else {
        setInstallError(response.Message || 'Update failed');
        logger.error('Update failed:', response.Message);
      }
    } catch (err) {
      logger.error('Error updating server:', err);
      setInstallError(err.message || 'Failed to update server');
    } finally {
      setUpdating((prev) => ({ ...prev, [server.id]: false }));
    }
  };

  // Fetch on mount
  useEffect(() => {
    fetchMCPRegistry();
  }, []);

  // Debounced search
  useEffect(() => {
    const timer = setTimeout(() => {
      fetchMCPRegistry(searchQuery);
    }, 500);

    return () => clearTimeout(timer);
  }, [searchQuery]);

  return (
    <>
      <AuthConfigModal
        visible={showAuthModal}
        onDismiss={() => {
          setShowAuthModal(false);
          setPendingServer(null);
        }}
        onSubmit={handleAuthSubmit}
        server={pendingServer}
      />

      <Container
        header={
          <Header
            variant="h3"
            description="Browse and install MCP servers from the official Model Context Protocol registry"
            actions={
              <Button onClick={() => fetchMCPRegistry(searchQuery)} iconName="refresh" disabled={loading}>
                Refresh
              </Button>
            }
          >
            Public MCP Servers
          </Header>
        }
      >
        <SpaceBetween size="m">
          {installSuccess && (
            <Alert type="success" dismissible onDismiss={() => setInstallSuccess(null)} header="Installation Started">
              {installSuccess}
              <Box margin={{ top: 's' }}>
                The MCP server layer is being built. This may take 2-3 minutes. The server will be available for use
                once the build completes.
              </Box>
            </Alert>
          )}

          {installError && (
            <Alert type="error" dismissible onDismiss={() => setInstallError(null)} header="Installation Failed">
              {installError}
            </Alert>
          )}

          <Alert type="info">
            Browse servers from the{' '}
            <Link external href="https://registry.modelcontextprotocol.io">
              official MCP registry
            </Link>
            . Install servers to add new capabilities to your meeting assistant. Maximum 5 servers can be enabled.
          </Alert>

          {/* Search */}
          <FormField label="Search servers" description="Searches all servers in the official MCP registry">
            <Input
              placeholder="Try: calendar, slack, database, filesystem..."
              value={searchQuery}
              onChange={({ detail }) => setSearchQuery(detail.value)}
              disabled={loading}
              type="search"
              clearAriaLabel="Clear search"
            />
          </FormField>

          {loading && !loadingMore && (
            <Box textAlign="center" padding="xxl">
              <Spinner size="large" />
              <Box margin={{ top: 's' }}>Loading MCP servers from registry...</Box>
            </Box>
          )}

          {error && (
            <Alert type="error" header="Error Loading Registry">
              {error}
              <Box margin={{ top: 's' }}>
                <Button onClick={() => fetchMCPRegistry(searchQuery)} iconName="refresh">
                  Retry
                </Button>
              </Box>
            </Alert>
          )}

          {!loading && !error && servers.length === 0 && (
            <Box textAlign="center" padding="xxl" color="text-body-secondary">
              No servers found{searchQuery && ` matching "${searchQuery}"`}
            </Box>
          )}

          {!loading && !error && servers.length > 0 && (
            <>
              <SpaceBetween direction="horizontal" size="xs">
                <Box>
                  Showing {servers.length} server{servers.length !== 1 ? 's' : ''}
                </Box>
                {hasMore && <Box color="text-body-secondary">(More available)</Box>}
              </SpaceBetween>

              <ColumnLayout columns={2}>
                {servers.map((server) => {
                  const isInstalled = installedServerIds.has(server.id);
                  const installedServer = installedServerMap.get(server.id);
                  const hasUpdate = isInstalled && installedServer && installedServer.Version !== server.version;

                  return (
                    <Container key={server.id}>
                      <SpaceBetween size="s">
                        {/* Server Header */}
                        <Box>
                          <SpaceBetween direction="horizontal" size="xs">
                            <Box variant="h4">{server.name}</Box>
                            {server.verified && <Badge color="green">Verified</Badge>}
                            <Badge color={server.isSupported ? 'grey' : 'red'}>{server.packageType}</Badge>
                            {!server.isSupported && <Badge color="red">Unsupported</Badge>}
                            {isInstalled && <Badge color="green">Installed</Badge>}
                            {hasUpdate && <Badge color="blue">Update Available</Badge>}
                            {server.requiresAuth && <Badge color="blue">Requires Auth</Badge>}
                          </SpaceBetween>
                        </Box>

                        {/* Description */}
                        <Box fontSize="body-s" color="text-body-secondary">
                          {server.description}
                        </Box>

                        {/* Metadata */}
                        <ColumnLayout columns={2} variant="text-grid">
                          <Box>
                            <Box variant="awsui-key-label">Version</Box>
                            <Box>{server.version}</Box>
                          </Box>

                          <Box>
                            <Box variant="awsui-key-label">Transport</Box>
                            <Box>{server.transport.join(', ')}</Box>
                          </Box>

                          <Box>
                            <Box variant="awsui-key-label">Package</Box>
                            <Box>
                              <code style={{ fontSize: '11px' }}>{server.npmPackage}</code>
                            </Box>
                          </Box>
                        </ColumnLayout>

                        {/* Unsupported Package Type Warning */}
                        {!server.isSupported && (
                          <Alert type="warning" header="Unsupported Package Type">
                            This server uses {server.packageType} packages. Only PyPI (Python) packages are currently
                            supported. The STRANDS agent is Python-based and requires Python MCP servers.
                          </Alert>
                        )}

                        {/* Auth Requirements */}
                        {server.requiresAuth && server.isSupported && (
                          <Alert type="warning" header="Requires Authentication">
                            You&apos;ll need to provide credentials when installing this server.
                          </Alert>
                        )}

                        {/* Actions */}
                        <Box float="right">
                          <SpaceBetween direction="horizontal" size="xs">
                            <Button variant="link" iconName="external" href={server.homepage} target="_blank">
                              Documentation
                            </Button>
                            {isInstalled ? (
                              <>
                                {hasUpdate && (
                                  <Button
                                    variant="primary"
                                    onClick={() => handleUpdate(server)}
                                    loading={updating[server.id]}
                                    disabled={updating[server.id] || uninstalling[server.id]}
                                  >
                                    {updating[server.id] ? 'Updating...' : 'Update'}
                                  </Button>
                                )}
                                <Button
                                  onClick={() => handleUninstall(server)}
                                  loading={uninstalling[server.id]}
                                  disabled={uninstalling[server.id] || updating[server.id]}
                                >
                                  {uninstalling[server.id] ? 'Uninstalling...' : 'Uninstall'}
                                </Button>
                              </>
                            ) : (
                              <Button
                                variant="primary"
                                onClick={() => handleInstall(server)}
                                loading={installing[server.id]}
                                disabled={installing[server.id] || !server.isSupported}
                              >
                                {(() => {
                                  if (installing[server.id]) return 'Installing...';
                                  if (server.isSupported) return 'Install';
                                  return 'Unsupported';
                                })()}
                              </Button>
                            )}
                          </SpaceBetween>
                        </Box>
                      </SpaceBetween>
                    </Container>
                  );
                })}
              </ColumnLayout>

              {/* Load More Button */}
              {hasMore && (
                <Box textAlign="center" margin={{ top: 'm' }}>
                  <Button onClick={loadMore} loading={loadingMore} iconName="angle-down">
                    {loadingMore ? 'Loading More Servers...' : 'Load More Servers'}
                  </Button>
                </Box>
              )}
            </>
          )}
        </SpaceBetween>
      </Container>
    </>
  );
};

PublicRegistryTab.propTypes = {
  onInstall: PropTypes.func,
  installedServers: PropTypes.arrayOf(
    PropTypes.shape({
      ServerId: PropTypes.string,
      Name: PropTypes.string,
      Status: PropTypes.string,
    }),
  ),
};

PublicRegistryTab.defaultProps = {
  onInstall: () => {},
  installedServers: [],
};

export default PublicRegistryTab;
