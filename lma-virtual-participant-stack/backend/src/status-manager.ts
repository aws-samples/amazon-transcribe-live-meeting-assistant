import { DynamoDBClient, GetItemCommand, UpdateItemCommand } from '@aws-sdk/client-dynamodb';
import { marshall, unmarshall } from '@aws-sdk/util-dynamodb';
import { 
  ElasticLoadBalancingV2Client, 
  RegisterTargetsCommand, 
  DeregisterTargetsCommand,
  DescribeTargetHealthCommand 
} from '@aws-sdk/client-elastic-load-balancing-v2';
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
  private elbClient: ElasticLoadBalancingV2Client;
  private taskPrivateIp: string | null = null;

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

    this.elbClient = new ElasticLoadBalancingV2Client({
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

  // Method to get CallId from VP record
  async getCallId(): Promise<string | null> {
    try {
      const query = `
        query GetVirtualParticipant($id: ID!) {
          getVirtualParticipant(id: $id) {
            id
            CallId
          }
        }
      `;

      const result = await this.signAndSendGraphQLRequest(query, {
        id: this.participantId
      });
      
      if (result?.getVirtualParticipant?.CallId) {
        console.log(`Retrieved CallId ${result.getVirtualParticipant.CallId} for VP ${this.participantId}`);
        return result.getVirtualParticipant.CallId;
      } else {
        console.log(`No CallId found for VP ${this.participantId}`);
        return null;
      }

    } catch (error) {
      console.error('Error getting CallId:', error);
      return null;
    }
  }

  // Method to set CallId in VP record
  async setCallId(callId: string): Promise<boolean> {
    try {
      const mutation = `
        mutation UpdateVirtualParticipant($input: UpdateVirtualParticipantInput!) {
          updateVirtualParticipant(input: $input) {
            id
            CallId
            updatedAt
          }
        }
      `;

      const variables = {
        input: {
          id: this.participantId,
          status: 'SCHEDULED', // Keep current status
          CallId: callId
        }
      };

      const result = await this.signAndSendGraphQLRequest(mutation, variables);
      
      if (result) {
        console.log(`Successfully set CallId ${callId} for VP ${this.participantId}`);
        return true;
      } else {
        console.error('Failed to set CallId via GraphQL');
        return false;
      }

    } catch (error) {
      console.error('Error setting CallId:', error);
      return false;
    }
  }

  // Status update methods matching Python implementation
  async setScheduled(scheduledTime?: Date): Promise<boolean> {
    const message = scheduledTime ? `Scheduled for ${scheduledTime.toISOString()}` : undefined;
    return this.updateStatus('SCHEDULED', message);
  }

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

  async setManualActionRequired(
    actionType: string,
    message: string,
    timeoutSeconds: number
  ): Promise<boolean> {
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
      console.log(`Setting manual action required: ${actionType} - ${message}`);

      // Update with MANUAL_ACTION_REQUIRED status and metadata
      const mutation = `
        mutation UpdateVirtualParticipant($input: UpdateVirtualParticipantInput!) {
          updateVirtualParticipant(input: $input) {
            id
            status
            CallId
            vncEndpoint
            vncPort
            vncReady
            manualActionType
            manualActionMessage
            manualActionTimeoutSeconds
            manualActionStartTime
            updatedAt
          }
        }
      `;

      const variables: any = {
        input: {
          id: this.participantId,
          status: 'MANUAL_ACTION_REQUIRED',
          manualActionType: actionType,
          manualActionMessage: message,
          manualActionTimeoutSeconds: timeoutSeconds,
          manualActionStartTime: new Date().toISOString()
        }
      };

      // Preserve CallId if it exists
      if (current.CallId) {
        variables.input.CallId = current.CallId;
      }

      // Preserve VNC fields if they exist
      if (current.vncEndpoint) {
        variables.input.vncEndpoint = current.vncEndpoint;
        variables.input.vncPort = current.vncPort;
        variables.input.vncReady = current.vncReady;
      }

      const result = await this.signAndSendGraphQLRequest(mutation, variables);
      
      if (result) {
        console.log(`Successfully set manual action required for VP ${this.participantId}`);
        return true;
      } else {
        console.error('Failed to set manual action required via GraphQL');
        return false;
      }

    } catch (error) {
      console.error('Error setting manual action required:', error);
      return false;
    }
  }

  async clearManualAction(): Promise<boolean> {
    try {
      // Get current VP to preserve fields
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
      console.log(`Clearing manual action for VP ${this.participantId}`);

      // Update to clear manual action fields by setting them to null
      const mutation = `
        mutation UpdateVirtualParticipant($input: UpdateVirtualParticipantInput!) {
          updateVirtualParticipant(input: $input) {
            id
            status
            CallId
            vncEndpoint
            vncPort
            vncReady
            manualActionType
            manualActionMessage
            manualActionTimeoutSeconds
            manualActionStartTime
            updatedAt
          }
        }
      `;

      const variables: any = {
        input: {
          id: this.participantId,
          status: 'JOINING',
          manualActionType: null,
          manualActionMessage: null,
          manualActionTimeoutSeconds: null,
          manualActionStartTime: null
        }
      };

      // Preserve CallId if it exists
      if (current.CallId) {
        variables.input.CallId = current.CallId;
      }

      // Preserve VNC fields if they exist
      if (current.vncEndpoint) {
        variables.input.vncEndpoint = current.vncEndpoint;
        variables.input.vncPort = current.vncPort;
        variables.input.vncReady = current.vncReady;
      }

      const result = await this.signAndSendGraphQLRequest(mutation, variables);
      
      if (result) {
        console.log(`Successfully cleared manual action for VP ${this.participantId}`);
        return true;
      } else {
        console.error('Failed to clear manual action via GraphQL');
        return false;
      }

    } catch (error) {
      console.error('Error clearing manual action:', error);
      return false;
    }
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
   * Register this task's IP with the ALB target group
   */
  async registerWithTargetGroup(): Promise<boolean> {
    try {
      const targetGroupArn = process.env.VNC_TARGET_GROUP_ARN;
      if (!targetGroupArn) {
        console.error('VNC_TARGET_GROUP_ARN environment variable not set');
        return false;
      }

      // Get task private IP
      const privateIp = await this.getTaskPrivateIp();
      if (!privateIp) {
        console.error('Could not get task private IP');
        return false;
      }

      this.taskPrivateIp = privateIp;
      console.log(`Registering task IP ${privateIp} with target group ${targetGroupArn}`);

      const registerCommand = new RegisterTargetsCommand({
        TargetGroupArn: targetGroupArn,
        Targets: [
          {
            Id: privateIp,
            Port: 5901,
          },
        ],
      });

      await this.elbClient.send(registerCommand);
      console.log(`✓ Successfully registered ${privateIp} with target group`);

      // Wait for target to become healthy
      console.log('Waiting for target to become healthy...');
      const isHealthy = await this.waitForTargetHealthy(targetGroupArn, privateIp);
      
      if (!isHealthy) {
        console.error('Target did not become healthy within timeout');
        return false;
      }

      console.log('✓ Target is healthy and ready to receive traffic');
      
      // Additional delay to ensure ALB is fully ready to route WebSocket traffic
      // This prevents race condition where frontend tries to connect before ALB routing is stable
      console.log('Waiting additional 5 seconds for ALB routing to stabilize...');
      await new Promise(resolve => setTimeout(resolve, 5000));
      console.log('✓ ALB routing stabilization complete');
      
      return true;

    } catch (error) {
      console.error('Failed to register with target group:', error);
      return false;
    }
  }

  /**
   * Wait for target to become healthy in the target group
   */
  private async waitForTargetHealthy(targetGroupArn: string, targetId: string, maxWaitSeconds: number = 60): Promise<boolean> {
    const startTime = Date.now();
    const maxWaitMs = maxWaitSeconds * 1000;

    while (Date.now() - startTime < maxWaitMs) {
      try {
        const healthCommand = new DescribeTargetHealthCommand({
          TargetGroupArn: targetGroupArn,
          Targets: [
            {
              Id: targetId,
              Port: 5901,
            },
          ],
        });

        const healthResponse = await this.elbClient.send(healthCommand);
        
        if (healthResponse.TargetHealthDescriptions && healthResponse.TargetHealthDescriptions.length > 0) {
          const targetHealth = healthResponse.TargetHealthDescriptions[0];
          const state = targetHealth.TargetHealth?.State;
          
          console.log(`Target health state: ${state}`);
          
          if (state === 'healthy') {
            return true;
          }
          
          if (state === 'unhealthy') {
            console.error(`Target is unhealthy: ${targetHealth.TargetHealth?.Reason}`);
            return false;
          }
        }

        // Wait 2 seconds before checking again
        await new Promise(resolve => setTimeout(resolve, 2000));

      } catch (error) {
        console.error('Error checking target health:', error);
        await new Promise(resolve => setTimeout(resolve, 2000));
      }
    }

    console.error('Timeout waiting for target to become healthy');
    return false;
  }

  /**
   * Deregister this task from the ALB target group
   */
  async deregisterFromTargetGroup(): Promise<boolean> {
    try {
      const targetGroupArn = process.env.VNC_TARGET_GROUP_ARN;
      if (!targetGroupArn || !this.taskPrivateIp) {
        console.log('No target group ARN or task IP - skipping deregistration');
        return true;
      }

      console.log(`Deregistering task IP ${this.taskPrivateIp} from target group`);

      const deregisterCommand = new DeregisterTargetsCommand({
        TargetGroupArn: targetGroupArn,
        Targets: [
          {
            Id: this.taskPrivateIp,
            Port: 5901,
          },
        ],
      });

      await this.elbClient.send(deregisterCommand);
      console.log(`✓ Successfully deregistered ${this.taskPrivateIp} from target group`);
      return true;

    } catch (error) {
      console.error('Failed to deregister from target group:', error);
      return false;
    }
  }

  /**
   * Signal that VNC is ready via AppSync
   * Publishes the full VNC WebSocket URL with vpId path for multi-user routing
   * Should only be called AFTER task is registered with ALB and healthy
   */
  async setVncReady(): Promise<boolean> {
    try {
      console.log(`Signaling VNC ready for VP ${this.participantId}`);
      
      // Get CloudFront domain from environment variable (same as AppSync URL pattern)
      const cloudFrontDomain = process.env.CLOUDFRONT_DOMAIN;
      
      if (!cloudFrontDomain) {
        console.error('CLOUDFRONT_DOMAIN environment variable not set');
        return false;
      }
      
      // Construct full VNC URL with vpId path for multi-user routing
      const vncEndpoint = `wss://${cloudFrontDomain}/vnc/${this.participantId}`;
      
      console.log(`VNC WebSocket URL with path: ${vncEndpoint}`);
      
      // Get current VP to preserve CallId
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

      // Update VP with vncReady flag and full URL
      const mutation = `
        mutation UpdateVirtualParticipant($input: UpdateVirtualParticipantInput!) {
          updateVirtualParticipant(input: $input) {
            id
            status
            vncEndpoint
            vncReady
            CallId
            updatedAt
          }
        }
      `;

      const variables: any = {
        input: {
          id: this.participantId,
          vncEndpoint: vncEndpoint,
          vncReady: true,
          status: 'VNC_READY'
        }
      };

      // Preserve CallId if it exists
      if (currentCallId) {
        variables.input.CallId = currentCallId;
      }

      const result = await this.signAndSendGraphQLRequest(mutation, variables);
      
      if (result) {
        console.log(`✓ VNC ready with URL: ${vncEndpoint}`);
        return true;
      } else {
        console.error('Failed to publish VNC ready signal');
        return false;
      }

    } catch (error) {
      console.error('Failed to signal VNC ready:', error);
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
