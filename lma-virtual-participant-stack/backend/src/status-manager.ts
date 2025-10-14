import { DynamoDBClient, GetItemCommand, UpdateItemCommand } from '@aws-sdk/client-dynamodb';
import { marshall, unmarshall } from '@aws-sdk/util-dynamodb';
import { createSignedFetcher } from 'aws-sigv4-fetch';
import { details } from './details.js';

export interface VPTaskRegistryItem {
  vpId: string;
  taskArn: string;
  clusterArn: string;
  createdAt: string;
  taskStatus: string;
  expiresAt: number;
}

export class VirtualParticipantStatusManager {
  private participantId: string;
  private graphqlEndpoint: string;
  private awsRegion: string;
  private dynamoClient: DynamoDBClient;

  constructor(participantId: string) {
    this.participantId = participantId;
    this.graphqlEndpoint = process.env.GRAPHQL_ENDPOINT || '';
    this.awsRegion = process.env.AWS_REGION || 'us-east-1';
    
    if (!this.graphqlEndpoint) {
      console.warn('GRAPHQL_ENDPOINT not configured - GraphQL status updates disabled');
    }

    this.dynamoClient = new DynamoDBClient({
      region: this.awsRegion,
    });
  }

  private async signAndSendGraphQLRequest(query: string, variables: any): Promise<any> {
    if (!this.graphqlEndpoint) {
      console.log('GraphQL endpoint not configured - skipping GraphQL update');
      return null;
    }

    try {
      // Use AWS SigV4 signing (matching Python implementation)
      const signedFetch = createSignedFetcher({
        service: 'appsync',
        region: this.awsRegion,
      });

      const payload = JSON.stringify({ query, variables });
      
      const response = await signedFetch(this.graphqlEndpoint, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: payload,
      });

      if (!response.ok) {
        console.error(`Failed to send GraphQL request: HTTP ${response.status}`);
        return null;
      }

      const responseData = await response.json();
      
      if (responseData.errors) {
        console.error('GraphQL errors:', responseData.errors);
        throw new Error(`GraphQL errors: ${JSON.stringify(responseData.errors)}`);
      }

      return responseData.data;
    } catch (error) {
      console.error('Failed to send GraphQL request:', error);
      throw error;
    }
  }

  async updateStatus(status: string, errorMessage?: string): Promise<boolean> {
    try {
      // First, get the current VP to preserve existing CallId
      const getQuery = `
        query GetVirtualParticipant($id: ID!) {
          getVirtualParticipant(id: $id) {
            id
            status
            CallId
          }
        }
      `;

      const currentVP = await this.signAndSendGraphQLRequest(getQuery, {
        id: this.participantId
      });

      if (!currentVP?.getVirtualParticipant) {
        console.error(`VP ${this.participantId} not found`);
        return false;
      }

      const currentCallId = currentVP.getVirtualParticipant.CallId;
      console.log(`Preserving CallId: ${currentCallId} while updating status to ${status}`);

      // Update with new status while preserving CallId
      const mutation = `
        mutation UpdateVirtualParticipant($input: UpdateVirtualParticipantInput!) {
          updateVirtualParticipant(input: $input) {
            id
            status
            CallId
            updatedAt
          }
        }
      `;

      const variables: any = {
        input: {
          id: this.participantId,
          status: status
        }
      };

      // Include CallId if it exists
      if (currentCallId) {
        variables.input.CallId = currentCallId;
        console.log(`Including CallId in update: ${currentCallId}`);
      }

      if (status === 'FAILED' && errorMessage) {
        console.error(`VP ${this.participantId} failed: ${errorMessage}`);
      }

      const result = await this.signAndSendGraphQLRequest(mutation, variables);
      
      if (result) {
        console.log(`Successfully updated VP ${this.participantId} status to ${status} with preserved CallId: ${currentCallId}`);
        return true;
      } else {
        console.error('Failed to update VP status via GraphQL');
        return false;
      }

    } catch (error) {
      console.error('Unexpected error updating VP status:', error);
      return false;
    }
  }

  async storeTaskArnInRegistry(): Promise<void> {
    try {
      console.log(`Storing task ARN for VP ${this.participantId} in registry...`);

      // Get task ARN from ECS metadata endpoint
      const metadataUri = process.env.ECS_CONTAINER_METADATA_URI_V4;
      if (!metadataUri) {
        console.log('ECS_CONTAINER_METADATA_URI_V4 not found - not running in ECS');
        return;
      }

      // Get task metadata
      const taskMetadataUrl = `${metadataUri}/task`;
      console.log(`Fetching task metadata from: ${taskMetadataUrl}`);

      const response = await fetch(taskMetadataUrl);
      if (!response.ok) {
        throw new Error(`Failed to fetch task metadata: ${response.status}`);
      }

      const metadata = await response.json();
      const taskArn = metadata.TaskARN;
      const clusterArn = metadata.Cluster;

      console.log(`Retrieved task ARN: ${taskArn}`);
      console.log(`Retrieved cluster ARN: ${clusterArn}`);

      if (!taskArn || !clusterArn) {
        console.log('Could not get task ARN or cluster ARN from metadata');
        return;
      }

      // Store in VPTaskRegistry table
      const registryTableName = details.vpTaskRegistryTableName;
      if (!registryTableName) {
        console.log('VP_TASK_REGISTRY_TABLE_NAME environment variable not set');
        return;
      }

      console.log(`Using VPTaskRegistry table: ${registryTableName}`);

      // Calculate expiry time (24 hours from now)
      const expiryTime = Math.floor(Date.now() / 1000) + 86400;

      const putCommand = new UpdateItemCommand({
        TableName: registryTableName,
        Key: marshall({ vpId: this.participantId }),
        UpdateExpression: 'SET taskArn = :taskArn, clusterArn = :clusterArn, createdAt = :createdAt, taskStatus = :taskStatus, expiresAt = :expiresAt',
        ExpressionAttributeValues: marshall({
          ':taskArn': taskArn,
          ':clusterArn': clusterArn,
          ':createdAt': new Date().toISOString(),
          ':taskStatus': 'RUNNING',
          ':expiresAt': expiryTime,
        }),
      });

      await this.dynamoClient.send(putCommand);
      console.log(`✓ Successfully stored task ARN in registry for VP ${this.participantId}`);

    } catch (error) {
      console.error('Error storing task ARN in registry:', error);
      // This is non-critical - don't fail the container startup
    }
  }

  // Status update methods matching Python implementation
  async setInitializing(): Promise<boolean> {
    return this.updateStatus('INITIALIZING');
  }

  async setConnecting(): Promise<boolean> {
    return this.updateStatus('CONNECTING');
  }

  async setJoining(): Promise<boolean> {
    return this.updateStatus('JOINING');
  }

  async setJoined(): Promise<boolean> {
    const result = this.updateStatus('JOINED');
    if (!result) {
      throw new Error('Failed to update VP status to JOINED via GraphQL');
    }
    return result;
  }

  async setActive(): Promise<boolean> {
    return this.updateStatus('ACTIVE');
  }

  async setCompleted(): Promise<boolean> {
    return this.updateStatus('COMPLETED');
  }

  async setFailed(errorMessage?: string): Promise<boolean> {
    return this.updateStatus('FAILED', errorMessage);
  }

  /**
   * Get the task's private IP address from ECS metadata
   */
  async getTaskPrivateIp(): Promise<string | null> {
    try {
      const metadataUri = process.env.ECS_CONTAINER_METADATA_URI_V4;
      if (!metadataUri) {
        console.log('ECS_CONTAINER_METADATA_URI_V4 not found - not running in ECS');
        return null;
      }

      const taskMetadataUrl = `${metadataUri}/task`;
      console.log(`Fetching task metadata for IP from: ${taskMetadataUrl}`);

      const response = await fetch(taskMetadataUrl);
      if (!response.ok) {
        throw new Error(`Failed to fetch task metadata: ${response.status}`);
      }

      const metadata = await response.json();
      
      // Extract private IP from task metadata
      // The structure is: Containers[0].Networks[0].IPv4Addresses[0]
      if (metadata.Containers && metadata.Containers.length > 0) {
        const container = metadata.Containers[0];
        if (container.Networks && container.Networks.length > 0) {
          const network = container.Networks[0];
          if (network.IPv4Addresses && network.IPv4Addresses.length > 0) {
            const privateIp = network.IPv4Addresses[0];
            console.log(`Task private IP: ${privateIp}`);
            return privateIp;
          }
        }
      }

      console.log('Could not extract private IP from task metadata');
      return null;
    } catch (error) {
      console.error('Failed to get task private IP:', error);
      return null;
    }
  }

  /**
   * Publish VNC endpoint information via AppSync
   * The vncEndpoint will contain the full API Gateway WebSocket URL
   */
  async setVncReady(endpoint: string, port: number = 5901, password?: string): Promise<boolean> {
    try {
      // Get API Gateway WebSocket URL from LMA Settings
      const vncWebSocketUrl = process.env.VNC_WEBSOCKET_URL || '';
      
      console.log(`Publishing VNC endpoint for VP ${this.participantId}`);
      console.log(`Task IP: ${endpoint}:${port}`);
      console.log(`WebSocket URL: ${vncWebSocketUrl}`);

      // First, get the current VP to preserve existing fields
      const getQuery = `
        query GetVirtualParticipant($id: ID!) {
          getVirtualParticipant(id: $id) {
            id
            status
            CallId
          }
        }
      `;

      const currentVP = await this.signAndSendGraphQLRequest(getQuery, {
        id: this.participantId
      });

      if (!currentVP?.getVirtualParticipant) {
        console.error(`VP ${this.participantId} not found`);
        return false;
      }

      const currentCallId = currentVP.getVirtualParticipant.CallId;

      const mutation = `
        mutation UpdateVirtualParticipant($input: UpdateVirtualParticipantInput!) {
          updateVirtualParticipant(input: $input) {
            id
            status
            vncEndpoint
            vncPort
            vncReady
            CallId
            updatedAt
          }
        }
      `;

      const variables: any = {
        input: {
          id: this.participantId,
          vncEndpoint: vncWebSocketUrl || `${endpoint}:${port}`, // Use WebSocket URL if available, fallback to IP:port
          vncPort: port,
          vncReady: true,
          status: 'VNC_READY', // Optional intermediate status
        }
      };

      // Preserve CallId if it exists
      if (currentCallId) {
        variables.input.CallId = currentCallId;
      }

      // Include password if provided (for future use)
      if (password) {
        variables.input.vncPassword = password;
      }

      const result = await this.signAndSendGraphQLRequest(mutation, variables);
      
      if (result) {
        console.log(`✓ Successfully published VNC endpoint: ${vncWebSocketUrl || endpoint + ':' + port}`);
        return true;
      } else {
        console.error('Failed to publish VNC endpoint via GraphQL');
        return false;
      }

    } catch (error) {
      console.error('Failed to publish VNC endpoint:', error);
      return false;
    }
  }

  /**
   * Log VNC connection events for audit purposes
   */
  async logVncConnection(username: string, action: 'connect' | 'disconnect', clientIp?: string): Promise<void> {
    const logEntry = {
      vpId: this.participantId,
      username,
      action,
      timestamp: new Date().toISOString(),
      clientIp: clientIp || 'unknown',
    };
    
    // Log to CloudWatch (will be picked up by container logs)
    console.log('VNC_AUDIT:', JSON.stringify(logEntry));
  }
}

// Convenience functions for easy integration
export function createStatusManager(participantId: string): VirtualParticipantStatusManager {
  return new VirtualParticipantStatusManager(participantId);
}

export async function updateParticipantStatus(
  participantId: string, 
  status: string, 
  errorMessage?: string
): Promise<boolean> {
  const manager = new VirtualParticipantStatusManager(participantId);
  return manager.updateStatus(status, errorMessage);
}
