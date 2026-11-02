import { DynamoDBClient, GetItemCommand, UpdateItemCommand } from '@aws-sdk/client-dynamodb';
import { marshall, unmarshall } from '@aws-sdk/util-dynamodb';
import { 
  ElasticLoadBalancingV2Client, 
  RegisterTargetsCommand, 
  DeregisterTargetsCommand,
  DescribeTargetHealthCommand,
  CreateTargetGroupCommand,
  DeleteTargetGroupCommand,
  CreateRuleCommand,
  DeleteRuleCommand,
  DescribeRulesCommand,
  DescribeTargetGroupsCommand
} from '@aws-sdk/client-elastic-load-balancing-v2';
import { SecretsManagerClient, GetSecretValueCommand } from '@aws-sdk/client-secrets-manager';
import { createSignedFetcher } from 'aws-sigv4-fetch';
import { details } from './details.js';
import { isMicrovmLaunch, microvmVncEndpoint } from './launch-mode.js';
import { agendaFromVpRow, type VpAgenda } from './meeting-agenda.js';
import { createLogger } from './logger.js';

const log = createLogger('status-manager');

/**
 * Conditions for this participant's ALB listener rule.
 *
 * Both are required. The path selects this participant's own target group; the
 * origin-verify header establishes that the request arrived through this
 * deployment's own CloudFront distribution, which is the same condition the
 * template puts on the load balancer's own rule. Building them here, as a pure
 * function, is what lets a test assert that neither can go missing.
 */
export const buildListenerRuleConditions = (
  pathPattern: string,
  headerName: string,
  headerValue: string,
) => [
  {
    Field: 'path-pattern',
    Values: [pathPattern],
  },
  {
    Field: 'http-header',
    HttpHeaderConfig: {
      HttpHeaderName: headerName,
      Values: [headerValue],
    },
  },
];

export interface VPTaskRegistryItem {
  vpId: string;
  taskArn: string;
  clusterArn: string;
  createdAt: string;
  taskStatus: string;
  expiresAt: number;
}

/**
 * Every status a Virtual Participant row can carry.
 *
 * The set is a contract between four writers and one reader: this container
 * (most of them), `backend/entrypoint.sh` (BOOTING, before Node starts), the
 * Step Functions state machine in `lma-virtual-participant-stack/template.yaml`
 * (WAITING_FOR_CAPACITY, FAILED), the task reaper Lambda in the same template
 * (FAILED), and the React UI's `STATUS_CONFIG`, which renders a message, an
 * icon and a colour per status and shows the bare string for anything it does
 * not recognise. Declaring them here means a status this file writes is a value
 * the compiler has seen, rather than a string literal that only a deployed UI
 * can disprove.
 */
export const VP_STATUS = {
  SCHEDULED: 'SCHEDULED',
  INITIALIZING: 'INITIALIZING',
  WAITING_FOR_CAPACITY: 'WAITING_FOR_CAPACITY',
  BOOTING: 'BOOTING',
  REGISTERING_NETWORK: 'REGISTERING_NETWORK',
  VNC_READY: 'VNC_READY',
  HYDRATING_PROFILE: 'HYDRATING_PROFILE',
  LAUNCHING_BROWSER: 'LAUNCHING_BROWSER',
  WARMING_PROFILE: 'WARMING_PROFILE',
  CONNECTING: 'CONNECTING',
  JOINING: 'JOINING',
  JOINED: 'JOINED',
  ACTIVE: 'ACTIVE',
  COMPLETED: 'COMPLETED',
  FAILED: 'FAILED',
  ENDED: 'ENDED',
  CANCELLED: 'CANCELLED',
  MANUAL_ACTION_REQUIRED: 'MANUAL_ACTION_REQUIRED',
} as const;

export type VpStatus = (typeof VP_STATUS)[keyof typeof VP_STATUS];

/** All of them, for exhaustiveness checks in tests. */
export const VP_STATUSES: readonly VpStatus[] = Object.values(VP_STATUS);

/**
 * Statuses that say the VP is healthy: it is in the meeting, or it finished
 * normally. Reaching one of these means any status detail written earlier --
 * a join sub-step, or the reason an earlier attempt in this same run did not
 * work out -- no longer describes the participant, so it is cleared unless the
 * caller supplies a replacement.
 */
export const HEALTHY_VP_STATUSES: readonly VpStatus[] = [
  VP_STATUS.JOINED,
  VP_STATUS.ACTIVE,
  VP_STATUS.COMPLETED,
];

/**
 * Statuses from which a VP never moves on. The updateVirtualParticipant
 * resolver in `lma-ai-stack/deployment/lma-ai-stack.yaml` keeps the same set
 * (`$terminalStatuses`) to decide which writes it conditions on the row's end;
 * `vp-status-updates.test.ts` checks that the two agree.
 */
export const TERMINAL_VP_STATUSES: readonly VpStatus[] = [
  VP_STATUS.COMPLETED,
  VP_STATUS.FAILED,
  VP_STATUS.ENDED,
  VP_STATUS.CANCELLED,
];

export const isTerminalVpStatus = (status: string): boolean =>
  (TERMINAL_VP_STATUSES as readonly string[]).includes(status);

export const isVpStatus = (value?: string | null): value is VpStatus =>
  !!value && (VP_STATUSES as readonly string[]).includes(value);

/**
 * The status a manual-action prompt is about to interrupt, so it can be put
 * back afterwards. Null when there is nothing worth recording: an unrecognised
 * status, or MANUAL_ACTION_REQUIRED itself, which happens when a second prompt
 * arrives before the first has been cleared and must not displace the status
 * the first one recorded.
 */
export const statusToResume = (currentStatus?: string | null): VpStatus | null =>
  isVpStatus(currentStatus) && currentStatus !== VP_STATUS.MANUAL_ACTION_REQUIRED
    ? currentStatus
    : null;

/**
 * Where a VP goes when its manual-action prompt is cleared.
 *
 * A prompt displaces the status the VP was reporting, and the UI's alert stays
 * up until the status moves off MANUAL_ACTION_REQUIRED, so clearing a prompt
 * has to put a status back. The one recorded when the prompt was raised is the
 * right answer; JOINING is the fallback for a row written before this was
 * recorded, since every caller that raises a prompt pre-admission is in the
 * middle of joining.
 */
export const resumedStatus = (recordedStatus?: string | null): VpStatus =>
  statusToResume(recordedStatus) ?? VP_STATUS.JOINING;

/**
 * Whether a GraphQL failure is DynamoDB declining a conditional write.
 *
 * AppSync surfaces it as an error naming `ConditionalCheckFailedException`,
 * which for the write that restores a prompt's interrupted status is an
 * expected outcome rather than a fault: it says the row stopped prompting
 * before the write landed.
 */
export const isConditionalCheckFailure = (error: unknown): boolean =>
  /ConditionalCheckFailed/i.test(error instanceof Error ? error.message : String(error));

/**
 * Whether a status write should take the status-detail string off the row.
 *
 * `errorMessage` is deliberately overloaded: it is the detail line for every
 * status, not only failures -- the exit reason on COMPLETED ("Asked to leave by
 * Jeremy."), and the join sub-step while the VP is still JOINING. Because the
 * mutation leaves fields it was not given alone, a detail written during one
 * phase stays on the row through every later write that does not mention it.
 * So a transition into a healthy state with nothing new to say clears it; a
 * transition that carries its own detail just overwrites it.
 */
export const shouldClearStatusDetail = (status: string, statusDetail?: string): boolean =>
  !statusDetail && (HEALTHY_VP_STATUSES as readonly string[]).includes(status);

/**
 * The fields every mutation in this file selects, shared so that no write from
 * the container can be narrower than the UI is subscribed to.
 *
 * An AppSync subscription delivers the fields the *mutation* selected: one the
 * subscriber asked for that the mutation did not return arrives as null. Most
 * of the UI merges an update into what it already has and treats a null as "no
 * news", but `ManualActionAlerts` builds an alert out of the payload, so what a
 * write leaves out it renders as missing — which is why its alerts named the
 * meeting as "null" until this list included `meetingName`, and why a write
 * without the prompt fields would describe an open prompt in generic terms.
 *
 * Carrying the full set is this side's half of that. The other half is in the
 * component: a write while a prompt is open is normal — a join sub-step, the
 * CallId link — and each one returns the open prompt along with whatever it
 * changed, so the component compares an incoming prompt with the one on screen
 * and acts only on a new one.
 *
 * One list rather than per-mutation lists is the point: the next write added
 * here cannot forget. `vp-status-updates.test.ts` checks this list against the
 * subscription's own selection set, read from the UI, and checks that every
 * mutation in this file uses it.
 */
const SUBSCRIBED_FIELDS = `
            id
            meetingName
            meetingPlatform
            status
            Owner
            SharedWith
            manualActionType
            manualActionMessage
            manualActionTimeoutSeconds
            manualActionStartTime
            errorMessage
            updatedAt`;

/**
 * Whether this process has begun to shut down. Module state rather than a
 * field, because it describes the process: the platform handlers create a
 * manager of their own for each write, and every one of them has to see it.
 */
let processShuttingDown = false;

/**
 * Record that this process has begun to shut down, so it stops reporting
 * progress.
 *
 * Called from the signal handler and the emergency cleanup. From then on no
 * manager in this process writes a progress status, a join sub-step, a
 * manual-action prompt or the clearing of one; only a terminal status goes
 * through. Whatever the setup or join code was in the middle of carries on
 * running until the process exits, and each progress write it would otherwise
 * make describes a participant that is no longer going to get there. A
 * terminal status is still written, because recording how the participant
 * ended is what the shutdown path is for.
 *
 * This covers writes that start after the signal, and those whose row read
 * was still in flight when it came. One whose mutation was already on its way
 * to AppSync is the resolver's to decline: a progress status does not land on
 * a row whose end has been recorded (see the updateVirtualParticipant
 * resolver).
 */
export const beginShutdown = (): void => {
  processShuttingDown = true;
};

export class VirtualParticipantStatusManager {
  private participantId: string;
  private graphqlEndpoint: string;
  private awsRegion: string;
  private dynamoClient: DynamoDBClient;
  private elbClient: ElasticLoadBalancingV2Client;
  private secretsClient: SecretsManagerClient;
  private taskPrivateIp: string | null = null;
  private targetGroupArn: string | null = null;
  private listenerRuleArn: string | null = null;
  // Read once and held for the life of the task: the value is generated per
  // deployment, so there is nothing to refresh, and a listener rule is created
  // once per participant anyway.
  private originVerifyHeaderValue: string | null = null;

  constructor(participantId: string) {
    this.participantId = participantId;
    this.graphqlEndpoint = process.env.GRAPHQL_ENDPOINT || '';
    // AWS_REGION is reserved for MicroVM images, so the launcher passes the
    // region as VP_AWS_REGION. Prefer whatever the runtime set, then ours.
    this.awsRegion = process.env.AWS_REGION || process.env.VP_AWS_REGION || 'us-east-1';
    
    if (!this.graphqlEndpoint) {
      log.warn('GRAPHQL_ENDPOINT not configured - GraphQL status updates disabled');
    }

    this.dynamoClient = new DynamoDBClient({
      region: this.awsRegion,
    });

    this.elbClient = new ElasticLoadBalancingV2Client({
      region: this.awsRegion,
    });

    this.secretsClient = new SecretsManagerClient({
      region: this.awsRegion,
    });
  }

  private async signAndSendGraphQLRequest(query: string, variables: any): Promise<any> {
    if (!this.graphqlEndpoint) {
      log.info('GraphQL endpoint not configured - skipping GraphQL update');
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
        log.error(`Failed to send GraphQL request: HTTP ${response.status}`);
        return null;
      }

      const responseData = await response.json();
      
      if (responseData.errors) {
        log.error('GraphQL errors:', responseData.errors);
        throw new Error(`GraphQL errors: ${JSON.stringify(responseData.errors)}`);
      }

      return responseData.data;
    } catch (error) {
      log.error('Failed to send GraphQL request:', error);
      throw error;
    }
  }

  /** Whether a progress write should be skipped because shutdown has begun. */
  private skipProgressWrite(what: string): boolean {
    if (!processShuttingDown) return false;
    log.info(`VP ${this.participantId} is shutting down — not writing ${what}`);
    return true;
  }

  /**
   * Write a lifecycle status, and with it the detail line the UI shows.
   *
   * `endTrigger` is the structured counterpart of `errorMessage`: the exact
   * branch the VP exited through (`ExitInfo.trigger`, e.g.
   * 'pre-join:not-admitted'). It is persisted alongside the prose so join
   * outcomes can be counted with a query rather than a log search.
   */
  async updateStatus(
    status: VpStatus,
    errorMessage?: string,
    endTrigger?: string,
  ): Promise<boolean> {
    if (!isTerminalVpStatus(status) && this.skipProgressWrite(`status ${status}`)) {
      return false;
    }
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
        log.error(`VP ${this.participantId} not found`);
        return false;
      }

      const current = currentVP.getVirtualParticipant;
      log.info(`Current VP state from query:`, JSON.stringify(current, null, 2));
      log.info(`Preserving CallId: ${current.CallId}, VNC fields while updating status to ${status}`);

      // Reported, not refused. A VP's own container is the authority on its
      // progress, and a terminal status can arrive from elsewhere — the
      // launcher state machine and the task reaper both write FAILED — so a
      // task that is in fact still running has to be able to carry on saying
      // so. But the container never moves on from a terminal status under its
      // own steam, so seeing one here means two writers disagreed about this
      // participant, and that is worth a line in the log.
      if (isTerminalVpStatus(current.status) && !isTerminalVpStatus(status)) {
        log.warn(
          `VP ${this.participantId} is ${current.status} and is being set to ${status}`,
        );
      }

      // Checked again after the read, which is where a write started before a
      // shutdown spends most of its time.
      if (!isTerminalVpStatus(status) && this.skipProgressWrite(`status ${status}`)) {
        return false;
      }

      // Update with new status while preserving CallId and VNC fields.
      const mutation = `
        mutation UpdateVirtualParticipant($input: UpdateVirtualParticipantInput!) {
          updateVirtualParticipant(input: $input) {${SUBSCRIBED_FIELDS}
            CallId
            vncEndpoint
            vncPort
            vncReady
            endTrigger
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
        log.info(`Including CallId in update: ${current.CallId}`);
      }

      // Preserve VNC fields if they exist
      if (current.vncEndpoint) {
        variables.input.vncEndpoint = current.vncEndpoint;
        variables.input.vncPort = current.vncPort;
        variables.input.vncReady = current.vncReady;
        log.info(`Preserving VNC fields: ${current.vncEndpoint}:${current.vncPort}, ready: ${current.vncReady}`);
      }

      // The schema's `errorMessage` field is the generic status detail —
      // it's persisted on FAILED *and* on normal terminal states like
      // COMPLETED so the UI can show why the meeting actually ended
      // (e.g. "Asked to leave by Jeremy"), and it carries the JOINING
      // sub-step while the VP is still working its way in. Without this
      // assignment the mutation would only carry the status code itself
      // and the detail string would be silently dropped.
      //
      // The mutation leaves out fields it was not given, so a detail has to
      // be taken off the row explicitly once it no longer applies: reaching
      // a healthy state with nothing new to say clears it, which is what
      // keeps a message from an earlier attempt out of the UI for a VP that
      // went on to join successfully.
      if (errorMessage) {
        variables.input.errorMessage = errorMessage;
      } else if (shouldClearStatusDetail(status)) {
        variables.input.clearErrorMessage = true;
        log.info(`Clearing stale status detail on transition to ${status}`);
      }

      if (endTrigger) {
        variables.input.endTrigger = endTrigger;
      }

      if (status === VP_STATUS.FAILED && errorMessage) {
        log.error(`VP ${this.participantId} failed: ${errorMessage}`);
      }

      let result;
      try {
        result = await this.signAndSendGraphQLRequest(mutation, variables);
      } catch (error) {
        if (!isTerminalVpStatus(status) && isConditionalCheckFailure(error)) {
          // The resolver declines a progress status on a row whose end has
          // been recorded: the user asked for this participant to end, and
          // that outcome stands over any progress it reports afterwards.
          log.info(
            `VP ${this.participantId} has been ended — not setting its status to ${status}`,
          );
          return false;
        }
        throw error;
      }

      if (result) {
        log.info(`Successfully updated VP ${this.participantId} status to ${status} with preserved fields`);
        return true;
      } else {
        log.error('Failed to update VP status via GraphQL');
        return false;
      }

    } catch (error) {
      log.error('Unexpected error updating VP status:', error);
      return false;
    }
  }

  async storeTaskArnInRegistry(): Promise<void> {
    try {
      log.info(`Storing task ARN and VNC endpoint for VP ${this.participantId} in registry...`);

      // Get task ARN and private IP from ECS metadata endpoint
      const metadataUri = process.env.ECS_CONTAINER_METADATA_URI_V4;
      if (!metadataUri) {
        log.info('ECS_CONTAINER_METADATA_URI_V4 not found - not running in ECS');
        return;
      }

      // Get task metadata
      const taskMetadataUrl = `${metadataUri}/task`;
      log.info(`Fetching task metadata from: ${taskMetadataUrl}`);

      const response = await fetch(taskMetadataUrl);
      if (!response.ok) {
        throw new Error(`Failed to fetch task metadata: ${response.status}`);
      }

      const metadata = await response.json();
      const taskArn = metadata.TaskARN;
      const clusterArn = metadata.Cluster;

      log.info(`Retrieved task ARN: ${taskArn}`);
      log.info(`Retrieved cluster ARN: ${clusterArn}`);

      if (!taskArn || !clusterArn) {
        log.info('Could not get task ARN or cluster ARN from metadata');
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
            log.info(`Task private IP: ${taskPrivateIp}`);
          }
        }
      }

      // Store in VPTaskRegistry table
      const registryTableName = details.vpTaskRegistryTableName;
      if (!registryTableName) {
        log.info('VP_TASK_REGISTRY_TABLE_NAME environment variable not set');
        return;
      }

      log.info(`Using VPTaskRegistry table: ${registryTableName}`);

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
      log.info(`✓ Successfully stored task ARN and VNC endpoint in registry for VP ${this.participantId}`);

    } catch (error) {
      log.error('Error storing task ARN in registry:', error);
      // This is non-critical - don't fail the container startup
    }
  }

  // Read just the current status of this VP.
  async getCurrentStatus(): Promise<string | null> {
    try {
      const query = `
        query GetVirtualParticipant($id: ID!) {
          getVirtualParticipant(id: $id) {
            id
            status
          }
        }
      `;
      const result = await this.signAndSendGraphQLRequest(query, {
        id: this.participantId,
      });
      return result?.getVirtualParticipant?.status || null;
    } catch (error) {
      log.error('Error reading VP status:', error);
      return null;
    }
  }

  // True if the user has clicked "Got it" on the FAILED banner. The cleanup
  // path waits on this so the VNC viewer doesn't grey out the moment we
  // mark the VP failed — letting the user actually read the error message.
  async getUserAcknowledgedFailure(): Promise<boolean> {
    try {
      const query = `
        query GetVirtualParticipant($id: ID!) {
          getVirtualParticipant(id: $id) {
            id
            userAcknowledgedFailure
          }
        }
      `;
      const result = await this.signAndSendGraphQLRequest(query, {
        id: this.participantId,
      });
      return !!result?.getVirtualParticipant?.userAcknowledgedFailure;
    } catch (error) {
      log.error('Error reading userAcknowledgedFailure:', error);
      return false;
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
        log.info(`Retrieved CallId ${result.getVirtualParticipant.CallId} for VP ${this.participantId}`);
        return result.getVirtualParticipant.CallId;
      } else {
        log.info(`No CallId found for VP ${this.participantId}`);
        return null;
      }

    } catch (error) {
      log.error('Error getting CallId:', error);
      return null;
    }
  }

  /**
   * The agenda given when this VP was started or scheduled, from its own row.
   *
   * Null when the row has none or cannot be read: an agenda is an enhancement,
   * and a VP that could not read one still joins and records the meeting.
   */
  async getAgenda(): Promise<VpAgenda | null> {
    try {
      const query = `
        query GetVirtualParticipant($id: ID!) {
          getVirtualParticipant(id: $id) {
            id
            agenda
            agendaSource
          }
        }
      `;
      const result = await this.signAndSendGraphQLRequest(query, { id: this.participantId });
      const agenda = agendaFromVpRow(result?.getVirtualParticipant);
      log.info(
        agenda
          ? `VP ${this.participantId} has an agenda (${agenda.agenda.length} characters, source ${agenda.agendaSource})`
          : `VP ${this.participantId} has no agenda`,
      );
      return agenda;
    } catch (error) {
      log.error('Error getting the agenda:', error);
      return null;
    }
  }

  /**
   * Link this VP to the call it is recording.
   *
   * Sends the CallId and nothing else. `status` is deliberately absent: this
   * method owns the CallId, not the lifecycle, and the mutation writes only
   * the fields it is given — so whatever status the VP's own progress last
   * wrote stays in place.
   */
  async setCallId(callId: string): Promise<boolean> {
    try {
      const mutation = `
        mutation UpdateVirtualParticipant($input: UpdateVirtualParticipantInput!) {
          updateVirtualParticipant(input: $input) {${SUBSCRIBED_FIELDS}
            CallId
          }
        }
      `;

      const variables = {
        input: {
          id: this.participantId,
          CallId: callId
        }
      };

      const result = await this.signAndSendGraphQLRequest(mutation, variables);
      
      if (result) {
        log.info(`Successfully set CallId ${callId} for VP ${this.participantId}`);
        return true;
      } else {
        log.error('Failed to set CallId via GraphQL');
        return false;
      }

    } catch (error) {
      log.error('Error setting CallId:', error);
      return false;
    }
  }

  // Status update methods matching Python implementation
  async setScheduled(scheduledTime?: Date): Promise<boolean> {
    const message = scheduledTime ? `Scheduled for ${scheduledTime.toISOString()}` : undefined;
    return this.updateStatus(VP_STATUS.SCHEDULED, message);
  }

  async setInitializing(): Promise<boolean> {
    return this.updateStatus(VP_STATUS.INITIALIZING);
  }

  // Granular sub-states inside the INITIALIZING phase. Each maps to a
  // different long-running setup step so the UI can show progress
  // instead of staying on a generic "Setting up..." for ~60s.
  async setRegisteringNetwork(): Promise<boolean> {
    return this.updateStatus(VP_STATUS.REGISTERING_NETWORK);
  }

  async setLaunchingBrowser(): Promise<boolean> {
    return this.updateStatus(VP_STATUS.LAUNCHING_BROWSER);
  }

  async setHydratingProfile(): Promise<boolean> {
    return this.updateStatus(VP_STATUS.HYDRATING_PROFILE);
  }

  async setWarmingProfile(): Promise<boolean> {
    return this.updateStatus(VP_STATUS.WARMING_PROFILE);
  }

  async setConnecting(): Promise<boolean> {
    return this.updateStatus(VP_STATUS.CONNECTING);
  }

  async setJoining(): Promise<boolean> {
    return this.updateStatus(VP_STATUS.JOINING);
  }

  /**
   * Publish a human-readable sub-step so the long join span (sign-in →
   * navigate → prejoin → admission → chat) doesn't look frozen in the UI. The
   * detail line renders through the errorMessage channel; this also bumps
   * updatedAt. Best-effort — never throws or blocks the join.
   *
   * It writes no status. The row is already JOINING before the platform
   * handler starts (`index.ts` sets it ahead of `initialize()`), so there is
   * nothing for a sub-step to add, and writing one would not be harmless: the
   * dialog watchdog starts at the top of the same handler and runs
   * concurrently with the join, so a sub-step landing just after the watchdog
   * raised MANUAL_ACTION_REQUIRED would take the prompt off the screen while
   * the user is still needed to clear it.
   */
  async setJoiningSubstep(message: string): Promise<void> {
    if (this.skipProgressWrite('a join sub-step')) return;
    try {
      const mutation = `
        mutation UpdateVirtualParticipant($input: UpdateVirtualParticipantInput!) {
          updateVirtualParticipant(input: $input) {${SUBSCRIBED_FIELDS}
          }
        }
      `;
      await this.signAndSendGraphQLRequest(mutation, {
        input: {
          id: this.participantId,
          errorMessage: message,
        },
      });
    } catch {
      /* progress sub-message is best-effort */
    }
  }

  // Admission. Clears the join sub-step detail with it (no message is
  // supplied), so the UI stops showing "Waiting to be admitted…" next to a
  // participant that is by then in the meeting.
  async setJoined(): Promise<boolean> {
    const updated = await this.updateStatus(VP_STATUS.JOINED);
    if (!updated) {
      // Reported, not thrown: the VP is in the meeting and recording either
      // way, so a status write that did not land must not end the meeting.
      log.error('Failed to update VP status to JOINED via GraphQL');
    }
    return updated;
  }

  async setActive(): Promise<boolean> {
    return this.updateStatus(VP_STATUS.ACTIVE);
  }

  async setCompleted(statusMessage?: string, endTrigger?: string): Promise<boolean> {
    return this.updateStatus(VP_STATUS.COMPLETED, statusMessage, endTrigger);
  }

  async setFailed(errorMessage?: string, endTrigger?: string): Promise<boolean> {
    return this.updateStatus(VP_STATUS.FAILED, errorMessage, endTrigger);
  }

  /**
   * Ask the user to take over in the live view.
   *
   * MANUAL_ACTION_REQUIRED is a status, so the prompt displaces whatever the VP
   * was doing. A prompt can arrive anywhere from sign-in to mid-meeting, so the
   * displaced status is recorded on the row for `clearManualAction` to go back
   * to — there is no one status that is right to resume from.
   */
  async setManualActionRequired(
    actionType: string,
    message: string,
    timeoutSeconds: number
  ): Promise<boolean> {
    if (this.skipProgressWrite(`a ${actionType} prompt`)) return false;
    try {
      // First, get the current VP to preserve existing CallId and VNC fields,
      // and to record the status this prompt is interrupting.
      const getQuery = `
        query GetVirtualParticipant($id: ID!) {
          getVirtualParticipant(id: $id) {
            id
            status
            statusBeforeManualAction
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
        log.error(`VP ${this.participantId} not found`);
        return false;
      }

      const current = currentVP.getVirtualParticipant;
      if (this.skipProgressWrite(`a ${actionType} prompt`)) return false;
      log.info(`Setting manual action required: ${actionType} - ${message}`);

      // Update with MANUAL_ACTION_REQUIRED status and metadata.
      const mutation = `
        mutation UpdateVirtualParticipant($input: UpdateVirtualParticipantInput!) {
          updateVirtualParticipant(input: $input) {${SUBSCRIBED_FIELDS}
            CallId
            vncEndpoint
            vncPort
            vncReady
            statusBeforeManualAction
          }
        }
      `;

      const variables: any = {
        input: {
          id: this.participantId,
          status: VP_STATUS.MANUAL_ACTION_REQUIRED,
          manualActionType: actionType,
          manualActionMessage: message,
          manualActionTimeoutSeconds: timeoutSeconds,
          manualActionStartTime: new Date().toISOString()
        }
      };

      // Record what to resume, unless a prompt is already pending — a second
      // prompt arriving before the first is cleared must not overwrite the
      // stash with MANUAL_ACTION_REQUIRED itself.
      const interrupted = statusToResume(current.status);
      if (interrupted) {
        variables.input.statusBeforeManualAction = interrupted;
      }

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

      let result;
      try {
        result = await this.signAndSendGraphQLRequest(mutation, variables);
      } catch (error) {
        if (isConditionalCheckFailure(error)) {
          // MANUAL_ACTION_REQUIRED is a progress status, so the resolver
          // declines it on a row whose end has been recorded.
          log.info(`VP ${this.participantId} has been ended — not raising a ${actionType} prompt`);
          return false;
        }
        throw error;
      }

      if (result) {
        log.info(`Successfully set manual action required for VP ${this.participantId}`);
        return true;
      } else {
        log.error('Failed to set manual action required via GraphQL');
        return false;
      }

    } catch (error) {
      log.error('Error setting manual action required:', error);
      return false;
    }
  }

  /**
   * Take the manual-action prompt off the row once the user has dealt with it.
   *
   * The prompt's attributes are removed through the mutation's
   * `clearManualAction` flag: an omitted field means "leave this attribute
   * alone", so the prompt needs an explicit signal to come off rather than a
   * null value, which the mutation ignores.
   *
   * The status goes back to the one the prompt interrupted, recorded on the row
   * by `setManualActionRequired`. It is not a fixed value: this is called from
   * the Zoom sign-in flow, the Teams and Webex pre-join flows and the dialog
   * watchdog, which runs for the whole meeting — so a VP prompted mid-meeting
   * returns to mid-meeting rather than to a join that already happened.
   *
   * A VP that is not prompting keeps whatever status it has. That matters for
   * the callers that clear opportunistically, without knowing whether a prompt
   * was ever raised.
   *
   * Both decisions here are taken from a read of the row, and the row can move
   * between that read and the write — the same read-modify-write shape
   * `setCallId` exists to avoid. The difference is that the resolver makes both
   * forms of this write conditional on the row still being what the read saw:
   * restoring a status applies only while the row is still
   * MANUAL_ACTION_REQUIRED, and clearing without a status applies only while it
   * is not. Either way the writer with current information wins and this call
   * drops out, which it reports rather than retries.
   */
  async clearManualAction(): Promise<boolean> {
    // Clearing a prompt resumes a progress status, and after shutdown has
    // begun the terminal status being written supersedes the prompt anyway:
    // the UI shows a prompt only while the status is MANUAL_ACTION_REQUIRED.
    if (this.skipProgressWrite('the clearing of a prompt')) return false;
    try {
      log.info(`Clearing manual action for VP ${this.participantId}`);

      const getQuery = `
        query GetVirtualParticipant($id: ID!) {
          getVirtualParticipant(id: $id) {
            id
            status
            statusBeforeManualAction
          }
        }
      `;

      const currentVP = await this.signAndSendGraphQLRequest(getQuery, {
        id: this.participantId
      });

      if (!currentVP?.getVirtualParticipant) {
        log.error(`VP ${this.participantId} not found`);
        return false;
      }

      const current = currentVP.getVirtualParticipant;
      if (this.skipProgressWrite('the clearing of a prompt')) return false;

      const mutation = `
        mutation UpdateVirtualParticipant($input: UpdateVirtualParticipantInput!) {
          updateVirtualParticipant(input: $input) {${SUBSCRIBED_FIELDS}
            CallId
            vncEndpoint
            vncPort
            vncReady
          }
        }
      `;

      const variables: any = {
        input: {
          id: this.participantId,
          clearManualAction: true
        }
      };

      // Only a prompting VP gets a status written, and only the one the prompt
      // displaced. Supplying it is also what makes the write conditional on the
      // row still prompting (see the resolver), which is what keeps this read
      // from being able to undo a status written since.
      if (current.status === VP_STATUS.MANUAL_ACTION_REQUIRED) {
        variables.input.status = resumedStatus(current.statusBeforeManualAction);
        log.info(`Resuming VP ${this.participantId} at ${variables.input.status}`);
      }

      let result;
      try {
        result = await this.signAndSendGraphQLRequest(mutation, variables);
      } catch (error) {
        if (isConditionalCheckFailure(error)) {
          // Not a failure. The row stopped matching what the read above saw,
          // so another writer acted on more current information than this call
          // has, and the condition in the resolver let that writer win.
          log.info(
            variables.input.status
              ? // Was restoring a prompt's interrupted status; the VP has since
                // left MANUAL_ACTION_REQUIRED, and the status that took it
                // there is newer than the one being restored. The prompt is off
                // the screen either way, because the UI stops showing one as
                // soon as the status moves off MANUAL_ACTION_REQUIRED.
                `VP ${this.participantId} is no longer prompting — leaving its status alone`
              : // Was clearing opportunistically, having read a row that was
                // not prompting; one has been raised since, and removing its
                // description now would leave a prompt nothing can describe.
                `VP ${this.participantId} is now prompting — leaving the prompt in place`,
          );
          return true;
        }
        throw error;
      }

      if (result) {
        log.info(`Successfully cleared manual action for VP ${this.participantId}`);
        return true;
      } else {
        log.error('Failed to clear manual action via GraphQL');
        return false;
      }

    } catch (error) {
      log.error('Error clearing manual action:', error);
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
        log.info('ECS_CONTAINER_METADATA_URI_V4 not found - not running in ECS');
        return null;
      }

      const taskMetadataUrl = `${metadataUri}/task`;
      log.info(`Fetching task metadata for IP from: ${taskMetadataUrl}`);

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
            log.info(`Task private IP: ${privateIp}`);
            return privateIp;
          }
        }
      }

      log.info('Could not extract private IP from task metadata');
      return null;
    } catch (error) {
      log.error('Failed to get task private IP:', error);
      return null;
    }
  }


  /**
   * Generate a unique priority number for ALB listener rule based on vpId
   * Uses hash to ensure consistent priority for same vpId
   */
  private generateRulePriority(vpId: string): number {
    // Simple hash function to generate priority between 1000-50000
    let hash = 0;
    for (let i = 0; i < vpId.length; i++) {
      hash = ((hash << 5) - hash) + vpId.charCodeAt(i);
      hash = hash & hash; // Convert to 32bit integer
    }
    // Map to range 1000-50000
    return 1000 + Math.abs(hash % 49000);
  }

  /**
   * Create a dedicated target group for this VP
   */
  private async createVPTargetGroup(): Promise<string | null> {
    try {
      const vpcId = process.env.VPC_ID;
      if (!vpcId) {
        log.error('VPC_ID environment variable not set');
        return null;
      }

      // Target group name max 32 chars, must be unique
      const tgName = `vnc-${this.participantId.substring(0, 27)}`;
      
      log.info(`Creating target group: ${tgName}`);

      const createTGCommand = new CreateTargetGroupCommand({
        Name: tgName,
        Protocol: 'HTTP',
        Port: 5901,
        VpcId: vpcId,
        TargetType: 'ip',
        HealthCheckEnabled: true,
        HealthCheckProtocol: 'HTTP',
        HealthCheckPath: '/',
        HealthCheckIntervalSeconds: 30,
        HealthCheckTimeoutSeconds: 5,
        HealthyThresholdCount: 2,
        UnhealthyThresholdCount: 3,
        Tags: [
          { Key: 'VirtualParticipantId', Value: this.participantId },
          { Key: 'ManagedBy', Value: 'LMA-VirtualParticipant' }
        ]
      });

      const response = await this.elbClient.send(createTGCommand);
      
      if (response.TargetGroups && response.TargetGroups.length > 0) {
        const tgArn = response.TargetGroups[0].TargetGroupArn!;
        log.info(`✓ Created target group: ${tgArn}`);
        return tgArn;
      }

      log.error('Failed to create target group - no ARN returned');
      return null;

    } catch (error: any) {
      if (error.name === 'DuplicateTargetGroupName') {
        log.info('Target group already exists, attempting to find it...');
        try {
          const tgName = `vnc-${this.participantId.substring(0, 27)}`;
          const describeCommand = new DescribeTargetGroupsCommand({
            Names: [tgName]
          });
          const response = await this.elbClient.send(describeCommand);
          if (response.TargetGroups && response.TargetGroups.length > 0) {
            const tgArn = response.TargetGroups[0].TargetGroupArn!;
            log.info(`✓ Found existing target group: ${tgArn}`);
            return tgArn;
          }
        } catch (describeError) {
          log.error('Failed to find existing target group:', describeError);
        }
      }
      log.error('Failed to create target group:', error);
      return null;
    }
  }

  /**
   * The origin-verify header value, read from Secrets Manager once per task.
   *
   * The ARN arrives in the environment, never the value: a secret value in a task
   * definition's environment is readable by anything that can call
   * ecs:DescribeTaskDefinition.
   *
   * Returns null if the value cannot be established, which callers must treat as
   * "do not create a rule" rather than "create one without the condition".
   */
  private async getOriginVerifyHeaderValue(): Promise<string | null> {
    if (this.originVerifyHeaderValue) {
      return this.originVerifyHeaderValue;
    }

    const secretArn = process.env.VNC_ORIGIN_VERIFY_SECRET_ARN;
    if (!secretArn) {
      log.error('VNC_ORIGIN_VERIFY_SECRET_ARN not set - cannot build the listener rule');
      return null;
    }

    try {
      const response = await this.secretsClient.send(
        new GetSecretValueCommand({ SecretId: secretArn })
      );
      if (!response.SecretString) {
        log.error('Origin-verify secret has no string value');
        return null;
      }
      // Generated by CloudFormation as JSON with the value under `headersecret`
      // (GenerateSecretString / GenerateStringKey), which is also the key the
      // template's own listener rule resolves.
      const value = JSON.parse(response.SecretString).headersecret;
      if (!value) {
        log.error('Origin-verify secret does not contain a headersecret value');
        return null;
      }
      this.originVerifyHeaderValue = value;
      return value;
    } catch (error) {
      // Deliberately does not log the secret or the response.
      log.error('Could not read the origin-verify header value:', error);
      return null;
    }
  }

  /**
   * Remove any listener rule whose path-pattern is exactly this participant's path.
   *
   * A rule outlives the task that created it if that task ended without the
   * manager's cleanup running. Replacing rather than reusing means the rule in
   * force is always the one this code just built, and it also clears the priority
   * this participant is about to claim.
   *
   * The match is element-wise equality against the condition's values, so it
   * selects only a rule written for this one path. A rule carrying a broader
   * pattern -- `/vnc/*`, which is the load balancer's own rule, or another
   * participant's path -- is not equal to this path and is left in place.
   */
  private async removeStaleListenerRule(listenerArn: string, pathPattern: string): Promise<void> {
    try {
      const { Rules } = await this.elbClient.send(
        new DescribeRulesCommand({ ListenerArn: listenerArn })
      );
      for (const rule of Rules || []) {
        const matchesPath = (rule.Conditions || []).some((condition) => {
          if (condition.Field !== 'path-pattern') {
            return false;
          }
          // Both forms: the API documents Values and PathPatternConfig.Values as
          // alternative ways to express the same condition, and does not state
          // which of them a describe call populates.
          const values = [
            ...(condition.Values || []),
            ...(condition.PathPatternConfig?.Values || []),
          ];
          return values.includes(pathPattern);
        });
        if (!matchesPath || !rule.RuleArn || rule.IsDefault) {
          continue;
        }
        log.info(`Removing an existing listener rule for ${pathPattern}`);
        await this.elbClient.send(new DeleteRuleCommand({ RuleArn: rule.RuleArn }));
      }
    } catch (error) {
      // Not fatal on its own: if a rule really is in the way, the create below
      // fails and that is the error worth reporting.
      log.error('Could not check for an existing listener rule:', error);
    }
  }

  /**
   * Create ALB listener rule to route /vnc/{vpId} to this VP's target group
   */
  private async createListenerRule(targetGroupArn: string): Promise<string | null> {
    try {
      const listenerArn = process.env.ALB_LISTENER_ARN;
      if (!listenerArn) {
        log.error('ALB_LISTENER_ARN environment variable not set');
        return null;
      }

      const headerName = process.env.VNC_ORIGIN_VERIFY_HEADER_NAME;
      if (!headerName) {
        log.error('VNC_ORIGIN_VERIFY_HEADER_NAME not set - cannot build the listener rule');
        return null;
      }

      // Established before the rule is created, so there is no path on which a
      // rule gets created carrying only the path condition.
      const headerValue = await this.getOriginVerifyHeaderValue();
      if (!headerValue) {
        log.error('Origin-verify header value unavailable - not creating a listener rule');
        return null;
      }

      // Unchanged: the priority determines where this rule sits relative to the
      // load balancer's own rule, which must be evaluated last.
      const priority = this.generateRulePriority(this.participantId);
      const pathPattern = `/vnc/${this.participantId}`;

      await this.removeStaleListenerRule(listenerArn, pathPattern);

      log.info(`Creating listener rule with priority ${priority} for path ${pathPattern}`);

      const createRuleCommand = new CreateRuleCommand({
        ListenerArn: listenerArn,
        Priority: priority,
        Conditions: buildListenerRuleConditions(pathPattern, headerName, headerValue),
        Actions: [
          {
            Type: 'forward',
            TargetGroupArn: targetGroupArn
          }
        ],
        Tags: [
          { Key: 'VirtualParticipantId', Value: this.participantId },
          { Key: 'ManagedBy', Value: 'LMA-VirtualParticipant' }
        ]
      });

      const response = await this.elbClient.send(createRuleCommand);

      if (response.Rules && response.Rules.length > 0) {
        const ruleArn = response.Rules[0].RuleArn!;
        log.info(`✓ Created listener rule: ${ruleArn}`);
        return ruleArn;
      }

      log.error('Failed to create listener rule - no ARN returned');
      return null;

    } catch (error: any) {
      if (error.name === 'PriorityInUse') {
        log.error(`Priority conflict - trying alternate priority`);
        // Could implement retry with different priority here
      }
      log.error('Failed to create listener rule:', error);
      return null;
    }
  }

  /**
   * Register this task's IP with the ALB target group
   * Now creates dedicated target group and listener rule per VP
   */
  async registerWithTargetGroup(): Promise<boolean> {
    // MICROVM has no ALB: RunMicrovm returns a dedicated HTTPS endpoint which
    // the launcher publishes, and there is no ECS task metadata endpoint to
    // read a private IP from. Report success so callers proceed.
    if (isMicrovmLaunch(process.env.VP_LAUNCH_TYPE)) {
      log.info('MICROVM launch: skipping ALB target-group registration');
      return true;
    }
    try {
      // Get task private IP
      const privateIp = await this.getTaskPrivateIp();
      if (!privateIp) {
        log.error('Could not get task private IP');
        return false;
      }

      this.taskPrivateIp = privateIp;
      log.info(`Task private IP: ${privateIp}`);

      // Step 1: Create dedicated target group for this VP
      log.info('Step 1: Creating dedicated target group...');
      const targetGroupArn = await this.createVPTargetGroup();
      if (!targetGroupArn) {
        log.error('Failed to create target group');
        return false;
      }
      this.targetGroupArn = targetGroupArn;

      // Step 2: Create listener rule to route /vnc/{vpId}/* to this target group
      log.info('Step 2: Creating listener rule...');
      const ruleArn = await this.createListenerRule(targetGroupArn);
      if (!ruleArn) {
        log.error('Failed to create listener rule');
        // Clean up target group
        await this.deleteVPTargetGroup();
        return false;
      }
      this.listenerRuleArn = ruleArn;

      // Step 3: Register this task's IP with the target group
      log.info(`Step 3: Registering task IP ${privateIp} with target group...`);
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
      log.info(`✓ Successfully registered ${privateIp} with target group`);

      // Step 4: Wait for target to become healthy
      log.info('Step 4: Waiting for target to become healthy...');
      const isHealthy = await this.waitForTargetHealthy(targetGroupArn, privateIp);
      
      if (!isHealthy) {
        log.error('Target did not become healthy within timeout');
        return false;
      }

      log.info('✓ Target is healthy and ready to receive traffic');
      
      // Additional delay to ensure ALB is fully ready to route WebSocket traffic
      log.info('Waiting additional 5 seconds for ALB routing to stabilize...');
      await new Promise(resolve => setTimeout(resolve, 5000));
      log.info('✓ ALB routing stabilization complete');
      
      log.info(`✓ VP ${this.participantId} fully registered with dedicated routing`);
      return true;

    } catch (error) {
      log.error('Failed to register with target group:', error);
      // Attempt cleanup
      await this.cleanupALBResources();
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
          
          log.info(`Target health state: ${state}`);
          
          if (state === 'healthy') {
            return true;
          }
          
          if (state === 'unhealthy') {
            log.error(`Target is unhealthy: ${targetHealth.TargetHealth?.Reason}`);
            return false;
          }
        }

        // Wait 2 seconds before checking again
        await new Promise(resolve => setTimeout(resolve, 2000));

      } catch (error) {
        log.error('Error checking target health:', error);
        await new Promise(resolve => setTimeout(resolve, 2000));
      }
    }

    log.error('Timeout waiting for target to become healthy');
    return false;
  }

  /**
   * Delete the listener rule created for this VP
   */
  private async deleteListenerRule(): Promise<boolean> {
    try {
      if (!this.listenerRuleArn) {
        log.info('No listener rule ARN to delete');
        return true;
      }

      log.info(`Deleting listener rule: ${this.listenerRuleArn}`);

      const deleteRuleCommand = new DeleteRuleCommand({
        RuleArn: this.listenerRuleArn
      });

      await this.elbClient.send(deleteRuleCommand);
      log.info(`✓ Deleted listener rule`);
      this.listenerRuleArn = null;
      return true;

    } catch (error: any) {
      if (error.name === 'RuleNotFound') {
        log.info('Listener rule already deleted');
        return true;
      }
      log.error('Failed to delete listener rule:', error);
      return false;
    }
  }

  /**
   * Delete the target group created for this VP
   */
  private async deleteVPTargetGroup(): Promise<boolean> {
    try {
      if (!this.targetGroupArn) {
        log.info('No target group ARN to delete');
        return true;
      }

      log.info(`Deleting target group: ${this.targetGroupArn}`);

      // First deregister all targets
      if (this.taskPrivateIp) {
        try {
          const deregisterCommand = new DeregisterTargetsCommand({
            TargetGroupArn: this.targetGroupArn,
            Targets: [{ Id: this.taskPrivateIp, Port: 5901 }]
          });
          await this.elbClient.send(deregisterCommand);
          log.info('✓ Deregistered targets from target group');
          
          // Wait a bit for deregistration to complete
          await new Promise(resolve => setTimeout(resolve, 2000));
        } catch (deregError) {
          log.info('Error deregistering targets (may already be deregistered):', deregError);
        }
      }

      const deleteTGCommand = new DeleteTargetGroupCommand({
        TargetGroupArn: this.targetGroupArn
      });

      await this.elbClient.send(deleteTGCommand);
      log.info(`✓ Deleted target group`);
      this.targetGroupArn = null;
      return true;

    } catch (error: any) {
      if (error.name === 'TargetGroupNotFound') {
        log.info('Target group already deleted');
        return true;
      }
      log.error('Failed to delete target group:', error);
      return false;
    }
  }

  /**
   * Clean up all ALB resources (listener rule and target group)
   */
  private async cleanupALBResources(): Promise<void> {
    log.info('Cleaning up ALB resources...');
    
    // Delete in correct order: rule first, then target group
    await this.deleteListenerRule();
    await this.deleteVPTargetGroup();
    
    log.info('✓ ALB resource cleanup complete');
  }

  /**
   * Deregister this task from the ALB target group and clean up resources
   */
  async deregisterFromTargetGroup(): Promise<boolean> {
    // Nothing was ever registered under MICROVM (see registerWithTargetGroup).
    // Returning early also avoids pointless ELBv2 calls on the teardown path,
    // which runs from three separate places including an emergency handler.
    if (isMicrovmLaunch(process.env.VP_LAUNCH_TYPE)) {
      return true;
    }
    try {
      log.info(`Deregistering VP ${this.participantId} from ALB`);
      
      // Clean up all ALB resources
      await this.cleanupALBResources();
      
      log.info(`✓ Successfully deregistered VP ${this.participantId}`);
      return true;

    } catch (error) {
      log.error('Failed to deregister from target group:', error);
      return false;
    }
  }

  /**
   * Signal that VNC is ready via AppSync
   * Publishes the full VNC WebSocket URL with vpId path for multi-user routing
   * Should only be called AFTER task is registered with ALB and healthy
   */
  /**
   * Read this VP's MicroVM endpoint from the task registry.
   *
   * Written by the launcher Lambda immediately after RunMicrovm. Retried
   * briefly because the container can reach this point before the launcher's
   * write lands.
   */
  async getMicrovmEndpointFromRegistry(attempts = 10, delayMs = 1000): Promise<string> {
    const tableName = process.env.VP_TASK_REGISTRY_TABLE_NAME;
    if (!tableName) {
      log.error('VP_TASK_REGISTRY_TABLE_NAME not set - cannot look up MicroVM endpoint');
      return '';
    }
    for (let attempt = 1; attempt <= attempts; attempt += 1) {
      try {
        const result = await this.dynamoClient.send(
          new GetItemCommand({
            TableName: tableName,
            Key: { vpId: { S: this.participantId } },
          }),
        );
        const endpoint = result.Item?.vncEndpoint?.S || '';
        if (endpoint) {
          log.info(`Resolved MicroVM endpoint from registry: ${endpoint}`);
          return endpoint;
        }
      } catch (error) {
        log.error('Error reading MicroVM endpoint from registry:', error);
      }
      if (attempt < attempts) {
        await new Promise((resolve) => setTimeout(resolve, delayMs));
      }
    }
    log.error(`MicroVM endpoint not in registry after ${attempts} attempts`);
    return '';
  }

  async setVncReady(): Promise<boolean> {
    try {
      log.info(`Signaling VNC ready for VP ${this.participantId}`);
      
      // Under MICROVM the browser connects straight to this MicroVM's own
      // endpoint (no CloudFront/ALB, no /vnc/<vpId> path routing). The launcher
      // passes it in as MICROVM_ENDPOINT; isolation comes from the per-MicroVM
      // endpoint plus a port-scoped auth token the UI supplies as a WebSocket
      // subprotocol.
      let vncEndpoint: string;
      if (isMicrovmLaunch(process.env.VP_LAUNCH_TYPE)) {
        // The endpoint only exists AFTER RunMicrovm returns, so it cannot be in
        // the /run payload (which is sent as part of that same call). The
        // launcher writes it to the VP task registry instead, and we read it
        // back here.
        vncEndpoint = microvmVncEndpoint(
          process.env.MICROVM_ENDPOINT || (await this.getMicrovmEndpointFromRegistry()),
        );
        if (!vncEndpoint) {
          log.error('No MicroVM endpoint available - cannot publish VNC endpoint');
          return false;
        }
      } else {
        // Get CloudFront domain from environment variable (same as AppSync URL pattern)
        const cloudFrontDomain = process.env.CLOUDFRONT_DOMAIN;

        if (!cloudFrontDomain) {
          log.error('CLOUDFRONT_DOMAIN environment variable not set');
          return false;
        }

        // Construct full VNC URL with vpId path for multi-user routing
        vncEndpoint = `wss://${cloudFrontDomain}/vnc/${this.participantId}`;
      }

      log.info(`VNC WebSocket URL with path: ${vncEndpoint}`);
      
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
        log.error(`VP ${this.participantId} not found`);
        return false;
      }

      const currentCallId = currentVP.getVirtualParticipant.CallId;

      // Update VP with vncReady flag and full URL.
      const mutation = `
        mutation UpdateVirtualParticipant($input: UpdateVirtualParticipantInput!) {
          updateVirtualParticipant(input: $input) {${SUBSCRIBED_FIELDS}
            CallId
            vncEndpoint
            vncReady
          }
        }
      `;

      // VNC_READY is a lifecycle step of its own, not a side effect of
      // publishing the endpoint: the UI shows it as "Live view ready / About
      // to navigate to the meeting URL". It is written once, from the single
      // point in the startup sequence that runs between profile warmup and
      // the first navigation, so it cannot land on a VP that is further along.
      const variables: any = {
        input: {
          id: this.participantId,
          vncEndpoint: vncEndpoint,
          vncReady: true,
          status: VP_STATUS.VNC_READY
        }
      };

      // Preserve CallId if it exists
      if (currentCallId) {
        variables.input.CallId = currentCallId;
      }

      const result = await this.signAndSendGraphQLRequest(mutation, variables);
      
      if (result) {
        log.info(`✓ VNC ready with URL: ${vncEndpoint}`);
        return true;
      } else {
        log.error('Failed to publish VNC ready signal');
        return false;
      }

    } catch (error) {
      log.error('Failed to signal VNC ready:', error);
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
    log.info('VNC_AUDIT:', JSON.stringify(logEntry));
  }
}

// Convenience functions for easy integration
export function createStatusManager(participantId: string): VirtualParticipantStatusManager {
  return new VirtualParticipantStatusManager(participantId);
}

export async function updateParticipantStatus(
  participantId: string,
  status: VpStatus,
  errorMessage?: string,
  endTrigger?: string
): Promise<boolean> {
  const manager = new VirtualParticipantStatusManager(participantId);
  return manager.updateStatus(status, errorMessage, endTrigger);
}
