/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import { ConsoleLogger } from 'aws-amplify/utils';
import { generateClient } from 'aws-amplify/api';
import React, { useEffect, useState } from 'react';
import { Container, Header, SpaceBetween } from '@cloudscape-design/components';
import MCPServersContent from '../mcp-servers/MCPServersContent';
import MCPApiKeySection from './MCPApiKeySection';
import { listVirtualParticipants, onUpdateVirtualParticipant } from '../../graphql/queries/virtualParticipantQueries';

const client = generateClient();
const logger = new ConsoleLogger('MCPServersPage');

/**
 * MCP Servers Configuration Page
 * Full-page view for managing MCP servers (admin only)
 */
const MCPServersPage = () => {
  const [vpData, setVpData] = useState(null);

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
      } finally {
        // Loading complete
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
      <MCPApiKeySection />
      <Container
        header={
          <Header variant="h1" description="Manage Model Context Protocol servers for the Strands agent">
            MCP Servers Configuration
          </Header>
        }
      >
        <MCPServersContent vpData={vpData} />
      </Container>
    </SpaceBetween>
  );
};

export default MCPServersPage;
