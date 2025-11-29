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
  Select,
  SpaceBetween,
  Spinner,
} from '@awsui/components-react';
import { Logger } from 'aws-amplify';

const logger = new Logger('PublicRegistryTab');

// GitHub raw content URL for MCP servers registry
const REGISTRY_URL = 'https://raw.githubusercontent.com/modelcontextprotocol/servers/main/src/index.json';

/**
 * Public Registry Tab - Browse and install MCP servers from Anthropic's registry
 */
const PublicRegistryTab = ({ onInstall }) => {
  const [servers, setServers] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [searchQuery, setSearchQuery] = useState('');
  const [categoryFilter, setCategoryFilter] = useState({ value: 'all', label: 'All Categories' });

  const fetchMCPRegistry = async () => {
    setLoading(true);
    setError(null);

    try {
      logger.info('Fetching MCP registry from GitHub...');

      const response = await fetch(REGISTRY_URL);

      if (!response.ok) {
        throw new Error(`Failed to fetch registry: ${response.statusText}`);
      }

      const data = await response.json();

      // Transform registry data to our format
      const serverList = Object.entries(data).map(([id, server]) => ({
        id,
        name: server.name || id.split('/').pop(),
        description: server.description || 'No description available',
        category: server.category || 'Other',
        npmPackage: id,
        transport: server.transport || ['stdio'],
        verified: true,
        requiresAuth: server.requiresAuth || false,
        tools: server.tools || [],
        homepage: server.homepage || `https://github.com/modelcontextprotocol/servers/tree/main/src/${id}`,
      }));

      setServers(serverList);
      logger.info(`Loaded ${serverList.length} MCP servers from registry`);
    } catch (err) {
      logger.error('Error fetching MCP registry:', err);
      setError(err.message || 'Failed to load MCP registry');
    } finally {
      setLoading(false);
    }
  };

  // Fetch MCP servers from GitHub on mount
  useEffect(() => {
    fetchMCPRegistry();
  }, []);

  // Filter servers by search and category
  const filteredServers = servers.filter((server) => {
    const matchesSearch =
      searchQuery === '' ||
      server.name.toLowerCase().includes(searchQuery.toLowerCase()) ||
      server.description.toLowerCase().includes(searchQuery.toLowerCase());

    const matchesCategory = categoryFilter.value === 'all' || server.category === categoryFilter.value;

    return matchesSearch && matchesCategory;
  });

  // Get unique categories from servers
  const categories = [
    { value: 'all', label: 'All Categories' },
    ...Array.from(new Set(servers.map((s) => s.category)))
      .sort()
      .map((cat) => ({ value: cat, label: cat })),
  ];

  return (
    <Container
      header={
        <Header
          variant="h3"
          description="Browse and install MCP servers from the official Model Context Protocol registry"
          actions={
            <Button onClick={fetchMCPRegistry} iconName="refresh" disabled={loading}>
              Refresh
            </Button>
          }
        >
          Public MCP Servers
        </Header>
      }
    >
      <SpaceBetween size="m">
        <Alert type="info">
          These servers are from{' '}
          <Link external href="https://github.com/modelcontextprotocol/servers">
            Anthropic&apos;s official MCP registry
          </Link>
          . Install servers to add new capabilities to your meeting assistant. Maximum 5 servers can be enabled.
        </Alert>

        {/* Search and Filter */}
        <ColumnLayout columns={2}>
          <FormField label="Search servers">
            <Input
              placeholder="Search by name or description..."
              value={searchQuery}
              onChange={({ detail }) => setSearchQuery(detail.value)}
              disabled={loading}
            />
          </FormField>

          <FormField label="Category">
            <Select
              selectedOption={categoryFilter}
              onChange={({ detail }) => setCategoryFilter(detail.selectedOption)}
              options={categories}
              disabled={loading}
            />
          </FormField>
        </ColumnLayout>

        {loading && (
          <Box textAlign="center" padding="xxl">
            <Spinner size="large" />
            <Box margin={{ top: 's' }}>Loading MCP servers from registry...</Box>
          </Box>
        )}

        {error && (
          <Alert type="error" header="Error Loading Registry">
            {error}
            <Box margin={{ top: 's' }}>
              <Button onClick={fetchMCPRegistry} iconName="refresh">
                Retry
              </Button>
            </Box>
          </Alert>
        )}

        {!loading && !error && filteredServers.length === 0 && (
          <Box textAlign="center" padding="xxl" color="text-body-secondary">
            No servers found matching your search criteria
          </Box>
        )}

        {!loading && !error && filteredServers.length > 0 && (
          <>
            <Box>
              Found {filteredServers.length} server{filteredServers.length !== 1 ? 's' : ''}
            </Box>

            <ColumnLayout columns={2}>
              {filteredServers.map((server) => (
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
                        <Box variant="awsui-key-label">Category</Box>
                        <Box>
                          <span className={`mcp-category-badge mcp-category-${server.category.toLowerCase()}`}>
                            {server.category}
                          </span>
                        </Box>
                      </Box>

                      <Box>
                        <Box variant="awsui-key-label">Tools</Box>
                        <Box>{server.tools.length} tools</Box>
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
                      <Alert type="warning" header={`Requires ${server.authType}`}>
                        You&apos;ll need to provide credentials when installing this server.
                      </Alert>
                    )}

                    {/* Actions */}
                    <Box float="right">
                      <SpaceBetween direction="horizontal" size="xs">
                        <Button variant="link" iconName="external" href={server.homepage} target="_blank">
                          Documentation
                        </Button>
                        <Button variant="primary" onClick={() => onInstall(server)} disabled>
                          Install
                        </Button>
                      </SpaceBetween>
                    </Box>
                  </SpaceBetween>
                </Container>
              ))}
            </ColumnLayout>
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
