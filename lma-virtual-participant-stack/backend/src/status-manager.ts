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
      // First, get the current VP to preserve existing CallId and VNC fields
      const getQuery = `
        query GetVirtualParticipant($id: ID!) {
          getVirtualParticipant(id: $id) {
            id
            status
            CallId
            vncEndpoint
            vncPort
            vncReady
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

      const current = currentVP.getVirtualParticipant;
      console.log(`Current VP state from query:`, JSON.stringify(current, null, 2));
      console.log(`Preserving CallId: ${current.CallId}, VNC fields while updating status to ${status}`);

      // Update with new status while preserving CallId and VNC fields
      const mutation = `
        mutation UpdateVirtualParticipant($input: UpdateVirtualParticipantInput!) {
          updateVirtualParticipant(input: $input) {
            id
            status
            CallId
            vncEndpoint
            vncPort
            vncReady
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

      // Preserve CallId if it exists
      if (current.CallId) {
        variables.input.CallId = current.CallId;
        console.log(`Including CallId in update: ${current.CallId}`);
      }

      // Preserve VNC fields if they exist
      if (current.vncEndpoint) {
        variables.input.vncEndpoint = current.vncEndpoint;
        variables.input.vncPort = current.vncPort;
        variables.input.vncReady = current.vncReady;
        console.log(`Preserving VNC fields: ${current.vncEndpoint}:${current.vncPort}, ready: ${current.vncReady}`);
      }

      if (status === 'FAILED' && errorMessage) {
        console.error(`VP ${this.participantId} failed: ${errorMessage}`);
      }

      const result = await this.signAndSendGraphQLRequest(mutation, variables);
      
      if (result) {
        console.log(`Successfully updated VP ${this.participantId} status to ${status} with preserved fields`);
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
      console.log(`Storing task ARN and VNC endpoint for VP ${this.participantId} in registry...`);

      // Get task ARN and private IP from ECS metadata endpoint
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

      // Extract private IP from task metadata
      let taskPrivateIp = null;
      if (metadata.Containers && metadata.Containers.length > 0) {
        const container = metadata.Containers[0];
        if (container.Networks && container.Networks.length > 0) {
          const network = container.Networks[0];
          if (network.IPv4Addresses && network.IPv4Addresses.length > 0) {
            taskPrivateIp = network.IPv4Addresses[0];
            console.log(`Task private IP: ${taskPrivateIp}`);
          }
        }
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

      const updateExpression = taskPrivateIp 
        ? 'SET taskArn = :taskArn, clusterArn = :clusterArn, createdAt = :createdAt, taskStatus = :taskStatus, expiresAt = :expiresAt, vncEndpoint = :vncEndpoint'
        : 'SET taskArn = :taskArn, clusterArn = :clusterArn, createdAt = :createdAt, taskStatus = :taskStatus, expiresAt = :expiresAt';

      const expressionValues: any = {
        ':taskArn': taskArn,
        ':clusterArn': clusterArn,
        ':createdAt': new Date().toISOString(),
        ':taskStatus': 'RUNNING',
        ':expiresAt': expiryTime,
      };

      if (taskPrivateIp) {
        expressionValues[':vncEndpoint'] = taskPrivateIp;
      }

      const putCommand = new UpdateItemCommand({
        TableName: registryTableName,
        Key: marshall({ vpId: this.participantId }),
        UpdateExpression: updateExpression,
        ExpressionAttributeValues: marshall(expressionValues),
      });

      await this.dynamoClient.send(putCommand);
      console.log(`✓ Successfully stored task ARN and VNC endpoint in registry for VP ${this.participantId}`);

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
   * Uses ALB DNS from VNC_ALB_DNS environment variable
   */
  async setVncReady(endpoint?: string, port: number = 443, password?: string): Promise<boolean> {
    try {
      // Get ALB DNS from environment variable (set by CloudFormation)
      const vncEndpoint = endpoint || process.env.VNC_ALB_DNS;
      
      if (!vncEndpoint) {
        console.error('VNC endpoint not provided and VNC_ALB_DNS environment variable not set');
        return false;
      }
      
      console.log(`Publishing VNC endpoint for VP ${this.participantId}`);
      console.log(`VNC Endpoint: ${vncEndpoint}:${port}`);

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
          vncEndpoint: vncEndpoint, // Public IP or ALB DNS
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

      console.log('Sending VNC mutation with variables:', JSON.stringify(variables, null, 2));
      const result = await this.signAndSendGraphQLRequest(mutation, variables);
      console.log('VNC mutation result:', JSON.stringify(result, null, 2));
      
      if (result) {
        console.log(`✓ Successfully published VNC endpoint: ${vncEndpoint}:${port}`);
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
