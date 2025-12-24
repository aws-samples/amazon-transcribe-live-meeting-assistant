/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React, { useEffect, useState } from 'react';
import { Container, Header, SpaceBetween } from '@awsui/components-react';
import { API, graphqlOperation, Logger } from 'aws-amplify';
import MCPServersContent from '../mcp-servers/MCPServersContent';
import { listVirtualParticipants, onUpdateVirtualParticipant } from '../../graphql/queries/virtualParticipantQueries';

const logger = new Logger('MCPServersPage');

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
        const result = await API.graphql(graphqlOperation(listVirtualParticipants));

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

    const subscription = API.graphql(graphqlOperation(onUpdateVirtualParticipant)).subscribe({
      next: ({ value }) => {
        const updated = value?.data?.onUpdateVirtualParticipant;
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
