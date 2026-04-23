/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import { ConsoleLogger } from 'aws-amplify/utils';
import { generateClient } from 'aws-amplify/api';
import React, { useEffect, useState } from 'react';
import { Container, Header, SpaceBetween, Tabs } from '@cloudscape-design/components';
import MCPServersContent from '../mcp-servers/MCPServersContent';
import HostedMcpAccessTab from './HostedMcpAccessTab';
import { listVirtualParticipants, onUpdateVirtualParticipant } from '../../graphql/queries/virtualParticipantQueries';

const client = generateClient();
const logger = new ConsoleLogger('MCPServersPage');

/**
 * MCP Servers Configuration Page
 *
 * Split into two tabs:
 *   1. "External MCP Servers" (default) — configure MCP servers that LMA's
 *      Strands agent connects OUT to (public registry + custom servers + VP).
 *   2. "Hosted MCP Access" — connection info for external MCP clients
 *      (Claude Desktop / Quick Suite / custom agents) to connect IN to
 *      LMA's own hosted MCP server (API key + OAuth). Surfaces the values
 *      that otherwise live only in CloudFormation stack outputs.
 */
const MCPServersPage = () => {
  const [vpData, setVpData] = useState(null);
  const [activeTabId, setActiveTabId] = useState('external-servers');

  // Fetch Virtual Participant data for active meetings
  useEffect(() => {
    const fetchVPData = async () => {
      try {
        const result = await client.graphql({ query: listVirtualParticipants });

        const vps = result.data.listVirtualParticipants || [];
        // Find the first active VP with VNC ready
        const activeVP = vps.find((vp) => vp.vncReady && vp.status === 'JOINED');

        if (activeVP) {
          setVpData(activeVP);
          logger.info('Found active VP:', activeVP);
        } else {
          setVpData(null);
        }
      } catch (error) {
        logger.error('Error fetching VP data:', error);
        setVpData(null);
      }
    };

    fetchVPData();
  }, []);

  // Subscribe to VP updates
  useEffect(() => {
    if (!vpData?.id) return undefined;

    const subscription = client.graphql({ query: onUpdateVirtualParticipant }).subscribe({
      next: (message) => {
        const updated = message?.data?.onUpdateVirtualParticipant;
        if (updated && updated.id === vpData.id) {
          setVpData((prev) => ({
            ...prev,
            ...updated,
          }));
          logger.info('VP updated:', updated);
        }
      },
      error: (err) => logger.error('VP subscription error:', err),
    });

    return () => subscription.unsubscribe();
  }, [vpData?.id]);

  return (
    <SpaceBetween size="l">
      <Header
        variant="h1"
        description={
          "Configure external MCP servers for LMA's Strands agent, or get the connection info " +
          "needed to access LMA's hosted MCP server from external clients like Amazon Quick Suite or Claude Desktop."
        }
      >
        MCP Servers
      </Header>
      <Tabs
        activeTabId={activeTabId}
        onChange={({ detail }) => setActiveTabId(detail.activeTabId)}
        tabs={[
          {
            id: 'external-servers',
            label: 'External MCP Servers',
            content: (
              <Container
                header={
                  <Header
                    variant="h2"
                    description={
                      'Install and manage MCP servers that the Strands agent can call out to ' +
                      '(public registry, custom servers, and Virtual Participant MCP).'
                    }
                  >
                    External MCP Servers Configuration
                  </Header>
                }
              >
                <MCPServersContent vpData={vpData} />
              </Container>
            ),
          },
          {
            id: 'hosted-access',
            label: 'Hosted MCP Access',
            content: <HostedMcpAccessTab />,
          },
        ]}
      />
    </SpaceBetween>
  );
};

export default MCPServersPage;
