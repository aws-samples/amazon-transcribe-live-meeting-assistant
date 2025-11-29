/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React, { useEffect, useState } from 'react';
import PropTypes from 'prop-types';
import {
  Alert,
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
  const [mcpTools, setMcpTools] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  const handleInstallServer = (server) => {
    logger.info('Install server requested:', server.id);
    // TODO: Implement server installation
    // Will add to DynamoDB and enable in Lambda
  };

  const fetchMCPTools = async () => {
    setLoading(true);
    setError(null);

    try {
      // GraphQL query to list MCP tools from VP
      const query = `
        query ListMCPTools($callId: ID!) {
          listMCPTools(CallId: $callId) {
            name
            description
            inputSchema
            category
          }
        }
      `;

      const result = await API.graphql(
        graphqlOperation(query, {
          callId: vpData.CallId,
        }),
      );

      const tools = result.data.listMCPTools || [];
      setMcpTools(tools);
      logger.info(`Fetched ${tools.length} MCP tools`);
    } catch (err) {
      logger.error('Error fetching MCP tools:', err);
      setError(err.message || 'Failed to fetch MCP tools');
    } finally {
      setLoading(false);
    }
  };

  // Fetch MCP tools when modal opens and VP is ready
  useEffect(() => {
    if (visible && vpData?.mcpReady) {
      fetchMCPTools();
    }
  }, [visible, vpData?.mcpReady, vpData?.CallId]);

  // Group tools by category
  const toolsByCategory = mcpTools.reduce((acc, tool) => {
    const category = tool.category || 'Other';
    if (!acc[category]) {
      acc[category] = [];
    }
    acc[category].push(tool);
    return acc;
  }, {});

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
                {/* Lambda MCP Servers - Always Available */}
                <Container
                  header={
                    <Header variant="h3" description="MCP servers running in the Strands agent (always available)">
                      Agent MCP Servers
                    </Header>
                  }
                >
                  <SpaceBetween size="m">
                    <Alert type="success" header="Available for All Meetings">
                      These MCP servers run in the Strands agent Lambda function and are available for both active and
                      completed meetings. They don&apos;t require a Virtual Participant.
                    </Alert>

                    <ColumnLayout columns={2}>
                      {/* GitHub MCP */}
                      <Container>
                        <SpaceBetween size="xs">
                          <Box>
                            <Box variant="h4">GitHub</Box>
                            <StatusIndicator type="info">Coming Soon</StatusIndicator>
                          </Box>
                          <Box fontSize="body-s">Search repositories, create issues, manage pull requests</Box>
                          <Box>
                            <Box variant="awsui-key-label">Tools</Box>
                            <Box>8 tools (search_repos, create_issue, list_prs, ...)</Box>
                          </Box>
                        </SpaceBetween>
                      </Container>

                      {/* Slack MCP */}
                      <Container>
                        <SpaceBetween size="xs">
                          <Box>
                            <Box variant="h4">Slack</Box>
                            <StatusIndicator type="info">Coming Soon</StatusIndicator>
                          </Box>
                          <Box fontSize="body-s">Send messages, list channels, search conversations</Box>
                          <Box>
                            <Box variant="awsui-key-label">Tools</Box>
                            <Box>6 tools (send_message, list_channels, search, ...)</Box>
                          </Box>
                        </SpaceBetween>
                      </Container>
                    </ColumnLayout>

                    <Alert type="info">Maximum 5 MCP servers can be enabled at once to maintain performance.</Alert>
                  </SpaceBetween>
                </Container>

                {/* VP MCP Server - Only During Active Meetings */}
                <Container
                  header={
                    <Header
                      variant="h3"
                      description="Chrome DevTools MCP for VP browser control (active meetings only)"
                      actions={
                        vpData?.mcpReady && (
                          <Button onClick={fetchMCPTools} iconName="refresh" disabled={loading}>
                            Refresh Tools
                          </Button>
                        )
                      }
                    >
                      Virtual Participant Browser Control
                    </Header>
                  }
                >
                  <SpaceBetween size="m">
                    <ColumnLayout columns={3} variant="text-grid">
                      <Box>
                        <Box variant="awsui-key-label">Status</Box>
                        <StatusIndicator type={vpData?.mcpReady ? 'success' : 'stopped'}>
                          {vpData?.mcpReady ? 'Connected' : 'Not Available'}
                        </StatusIndicator>
                      </Box>

                      <Box>
                        <Box variant="awsui-key-label">Communication</Box>
                        <Box>AppSync Subscriptions (Private)</Box>
                      </Box>

                      <Box>
                        <Box variant="awsui-key-label">Available Tools</Box>
                        <Box>{loading ? <Spinner size="normal" /> : `${mcpTools.length} tools`}</Box>
                      </Box>
                    </ColumnLayout>

                    {error && (
                      <Alert type="error" header="Error Loading Tools">
                        {error}
                      </Alert>
                    )}

                    {vpData?.mcpReady && !loading && mcpTools.length > 0 && (
                      <ExpandableSection header="View Available Tools" variant="container">
                        <SpaceBetween size="m">
                          {Object.entries(toolsByCategory).map(([category, tools]) => (
                            <Box key={category}>
                              <Box variant="h4" margin={{ bottom: 's' }}>
                                {category} ({tools.length} tools)
                              </Box>
                              <ColumnLayout columns={2}>
                                {tools.map((tool) => (
                                  <Box key={tool.name} padding={{ vertical: 'xs' }}>
                                    <Box>
                                      <code style={{ fontSize: '13px', fontWeight: 'bold', color: '#0073bb' }}>
                                        {tool.name}
                                      </code>
                                    </Box>
                                    <Box fontSize="body-s" color="text-body-secondary">
                                      {tool.description}
                                    </Box>
                                  </Box>
                                ))}
                              </ColumnLayout>
                            </Box>
                          ))}
                        </SpaceBetween>
                      </ExpandableSection>
                    )}

                    {!vpData?.mcpReady && (
                      <Alert type="info" header="MCP Server Not Available">
                        The MCP server will be available when a Virtual Participant joins this meeting. The VP must be
                        active and connected for browser control tools to work.
                      </Alert>
                    )}

                    {vpData?.mcpReady && !loading && mcpTools.length > 0 && (
                      <Alert type="success" header="Ready for Agent Control">
                        The Strands agent can now control the Virtual Participant&apos;s browser using natural language
                        commands. Try asking: &quot;navigate to amazon.com and take a screenshot&quot; or &quot;check
                        the performance of this website&quot;.
                      </Alert>
                    )}

                    {vpData?.mcpReady && !loading && mcpTools.length === 0 && !error && (
                      <Alert type="warning" header="No Tools Available">
                        The MCP server is connected but no tools were found. This may indicate a configuration issue.
                      </Alert>
                    )}
                  </SpaceBetween>
                </Container>
              </SpaceBetween>
            ),
          },
          {
            label: 'Public Registry',
            id: 'registry',
            content: <PublicRegistryTab onInstall={handleInstallServer} />,
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
