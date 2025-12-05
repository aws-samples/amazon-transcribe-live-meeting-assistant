/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React from 'react';
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
  StatusIndicator,
  Tabs,
} from '@awsui/components-react';
import { Logger } from 'aws-amplify';
import PublicRegistryTab from './PublicRegistryTab';
import './mcp-servers.css';

const logger = new Logger('MCPServersModal');

/**
 * MCP Servers Modal - Manage Model Context Protocol servers
 * Shows Lambda MCP servers (always available) and VP MCP (active meetings only)
 */
const MCPServersModal = ({ visible, onDismiss, vpData }) => {
  const handleInstallServer = (server) => {
    logger.info('Install server requested:', server.id);
    // TODO: Implement server installation
    // Will add to DynamoDB and enable in Lambda
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
                      {/* Outlook Calendar MCP */}
                      <Container>
                        <SpaceBetween size="xs">
                          <Box>
                            <Box variant="h4">Outlook Calendar</Box>
                            <StatusIndicator type="info">Coming Soon</StatusIndicator>
                          </Box>
                          <Box fontSize="body-s">Manage event listing, reading, and updates</Box>
                          <Box>
                            <Box variant="awsui-key-label">Tools</Box>
                            <Box>6 tools (create_event, delete_event, ...)</Box>
                          </Box>
                          <Box>
                            <Box variant="awsui-key-label">Endpoint</Box>
                            <Box>
                              <code style={{ fontSize: '11px' }}>https://outlook-calendar.mintmcp.com/mcp</code>
                            </Box>
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
