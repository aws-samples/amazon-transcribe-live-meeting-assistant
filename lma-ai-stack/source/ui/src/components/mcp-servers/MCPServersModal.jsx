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
} from '@awsui/components-react';
import { API, graphqlOperation, Logger } from 'aws-amplify';
import './mcp-servers.css';

const logger = new Logger('MCPServersModal');

/**
 * MCP Servers Modal - Manage Model Context Protocol servers
 * Dynamically fetches available tools from MCP servers via AppSync
 */
const MCPServersModal = ({ visible, onDismiss, vpData }) => {
  const [mcpTools, setMcpTools] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

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
      <SpaceBetween size="l">
        {/* Built-in VP MCP Server */}
        <Container
          header={
            <Header
              variant="h3"
              description="Chrome DevTools MCP server for Virtual Participant browser control"
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
                The MCP server will be available when a Virtual Participant joins this meeting. The VP must be active
                and connected for browser control tools to work.
              </Alert>
            )}

            {vpData?.mcpReady && !loading && mcpTools.length > 0 && (
              <Alert type="success" header="Ready for Agent Control">
                The Strands agent can now control the Virtual Participant&apos;s browser using natural language
                commands. Try asking: &quot;navigate to amazon.com and take a screenshot&quot; or &quot;check the
                performance of this website&quot;.
              </Alert>
            )}

            {vpData?.mcpReady && !loading && mcpTools.length === 0 && !error && (
              <Alert type="warning" header="No Tools Available">
                The MCP server is connected but no tools were found. This may indicate a configuration issue.
              </Alert>
            )}
          </SpaceBetween>
        </Container>

        {/* Future: Public Registry Tab */}
        {/* Future: Custom Servers Tab */}
      </SpaceBetween>
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
