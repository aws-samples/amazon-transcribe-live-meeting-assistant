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
