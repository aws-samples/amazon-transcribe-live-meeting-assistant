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

const logger = new Logger('PublicRegistryTab');

// Official MCP Registry API
const REGISTRY_API_BASE = 'https://registry.modelcontextprotocol.io/v0/servers';
const SERVERS_PER_PAGE = 50;

/**
 * Public Registry Tab - Browse and install MCP servers from official registry
 */
const PublicRegistryTab = ({ onInstall }) => {
  const [servers, setServers] = useState([]);
  const [loading, setLoading] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState(null);
  const [searchQuery, setSearchQuery] = useState('');
  const [nextCursor, setNextCursor] = useState(null);
  const [hasMore, setHasMore] = useState(false);
  const [installing, setInstalling] = useState({});
  const [installSuccess, setInstallSuccess] = useState(null);
  const [installError, setInstallError] = useState(null);

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
      let url = `${REGISTRY_API_BASE}?limit=${SERVERS_PER_PAGE}`;
      if (searchTerm) {
        url += `&search=${encodeURIComponent(searchTerm)}`;
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
          return meta?.isLatest;
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

          const npmPackage = server.packages?.[0]?.identifier || server.name;

          const requiresAuth =
            (server.remotes && server.remotes.some((r) => r.headers && r.headers.length > 0)) ||
            (server.packages &&
              server.packages.some((p) => p.environmentVariables && p.environmentVariables.some((v) => v.isSecret)));

          return {
            id: server.name,
            name: name.charAt(0).toUpperCase() + name.slice(1),
            description: server.description || 'No description available',
            category: 'Community',
            npmPackage,
            transport: transports.length > 0 ? transports : ['stdio'],
            verified: true,
            requiresAuth,
            tools: [],
            homepage: server.repository?.url || `https://registry.modelcontextprotocol.io/servers/${server.name}`,
            version: server.version,
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

  const handleInstall = async (server) => {
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
            Version: server.version,
            Transport: server.transport,
            RequiresAuth: server.requiresAuth,
          },
        }),
      );

      const response = result.data.installMCPServer;

      if (response.Success) {
        setInstallSuccess(`${server.name} installation started. Build ID: ${response.BuildId || 'N/A'}`);
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
              The MCP server layer is being built. This may take 2-3 minutes. The server will be available for use once
              the build completes.
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
              {servers.map((server) => (
                <Container key={server.id}>
                  <SpaceBetween size="s">
                    {/* Server Header */}
                    <Box>
                      <SpaceBetween direction="horizontal" size="xs">
                        <Box variant="h4">{server.name}</Box>
                        {server.verified && <Badge color="green">Verified</Badge>}
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

                    {/* Auth Requirements */}
                    {server.requiresAuth && (
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
                        <Button
                          variant="primary"
                          onClick={() => handleInstall(server)}
                          loading={installing[server.id]}
                          disabled={installing[server.id]}
                        >
                          {installing[server.id] ? 'Installing...' : 'Install'}
                        </Button>
                      </SpaceBetween>
                    </Box>
                  </SpaceBetween>
                </Container>
              ))}
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
  );
};

PublicRegistryTab.propTypes = {
  onInstall: PropTypes.func,
};

PublicRegistryTab.defaultProps = {
  onInstall: () => {},
};

export default PublicRegistryTab;
