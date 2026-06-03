/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import { ConsoleLogger } from 'aws-amplify/utils';
import { generateClient } from 'aws-amplify/api';
import React, { useState, useEffect } from 'react';
import { useParams, useNavigate, Link as RouterLink } from 'react-router-dom';
import PropTypes from 'prop-types';
import {
  SpaceBetween,
  Container,
  Header,
  ColumnLayout,
  Box,
  Button,
  Alert,
  Badge,
  Icon,
  Spinner,
  Flashbar,
} from '@cloudscape-design/components';
import useAppContext from '../../contexts/app';
import StatusTimeline from './StatusTimeline';
import VNCViewer from './VNCViewer';

const client = generateClient();
// VNC WebSocket URL is published by the backend in the vncEndpoint field
// Format: wss://{api-id}.execute-api.{region}.amazonaws.com/prod

const getVirtualParticipant = `
  query GetVirtualParticipant($id: ID!) {
    getVirtualParticipant(id: $id) {
      id
      meetingName
      meetingPlatform
      meetingId
      meetingTime
      scheduledFor
      isScheduled
      scheduleId
      status
      errorMessage
      createdAt
      updatedAt
      owner
      Owner
      SharedWith
      CallId
      vncEndpoint
      vncPort
      vncReady
      userAcknowledgedFailure
    }
  }
`;

const onUpdateVirtualParticipantDetailed = `
  subscription OnUpdateVirtualParticipant {
    onUpdateVirtualParticipant {
      id
      status
      errorMessage
      updatedAt
      meetingName
      owner
      Owner
      SharedWith
      CallId
      vncEndpoint
      vncPort
      vncReady
      manualActionType
      manualActionMessage
      manualActionTimeoutSeconds
      manualActionStartTime
      userAcknowledgedFailure
    }
  }
`;

const endVirtualParticipant = `
  mutation EndVirtualParticipant($input: EndVirtualParticipantInput!) {
    endVirtualParticipant(input: $input) {
      id
      status
      updatedAt
    }
  }
`;

const acknowledgeVPFailureMutation = `
  mutation AcknowledgeVPFailure($virtualParticipantId: ID!) {
    acknowledgeVPFailure(virtualParticipantId: $virtualParticipantId)
  }
`;

const logger = new ConsoleLogger('VirtualParticipantDetails');

// Status configuration with enhanced messaging
const STATUS_CONFIG = {
  SCHEDULED: {
    message: 'Scheduled for future execution',
    description: 'Virtual participant will automatically join at the scheduled time',
    icon: 'calendar',
    type: 'info',
    color: 'blue',
  },
  INITIALIZING: {
    message: 'Allocating compute…',
    description: 'Starting Fargate task and waiting for the headless browser stack to come up',
    icon: 'loading',
    type: 'in-progress',
    color: 'blue',
  },
  WAITING_FOR_CAPACITY: {
    message: 'Waiting for compute capacity…',
    description:
      'Task is queued waiting for an EC2 host slot. If the cluster is full, the auto-scaler will ' +
      'launch a new host (~60-90s); otherwise the task is just waiting briefly for placement.',
    icon: 'loading',
    type: 'in-progress',
    color: 'blue',
  },
  BOOTING: {
    message: 'Booting container…',
    description: 'Container started — pulling Chrome image, starting display, audio, and VNC server',
    icon: 'loading',
    type: 'in-progress',
    color: 'blue',
  },
  REGISTERING_NETWORK: {
    message: 'Registering network…',
    description:
      'Creating ALB target group and waiting for the live-view endpoint to become healthy (typically 30–60s)',
    icon: 'loading',
    type: 'in-progress',
    color: 'blue',
  },
  VNC_READY: {
    message: 'Live view ready',
    description: 'About to navigate to the meeting URL',
    icon: 'loading',
    type: 'in-progress',
    color: 'blue',
  },
  HYDRATING_PROFILE: {
    message: 'Restoring browser profile…',
    description: 'Downloading saved cookies / trusted-device markers from S3',
    icon: 'loading',
    type: 'in-progress',
    color: 'blue',
  },
  LAUNCHING_BROWSER: {
    message: 'Launching browser…',
    description: 'Starting the browser and platform extensions',
    icon: 'loading',
    type: 'in-progress',
    color: 'blue',
  },
  WARMING_PROFILE: {
    message: 'Warming new profile…',
    description: 'First-launch browsing pass before joining (one-time, ~15s)',
    icon: 'loading',
    type: 'in-progress',
    color: 'blue',
  },
  CONNECTING: {
    message: 'Connecting to meeting platform…',
    description: 'Establishing connection with meeting platform',
    icon: 'loading',
    type: 'in-progress',
    color: 'blue',
  },
  JOINING: {
    message: 'Joining meeting...',
    description: 'Attempting to enter the meeting room',
    icon: 'loading',
    type: 'in-progress',
    color: 'blue',
  },
  JOINED: {
    message: 'Successfully joined meeting',
    description: 'Virtual participant is now in the meeting',
    icon: 'status-positive',
    type: 'success',
    color: 'green',
  },
  ACTIVE: {
    message: 'Recording in progress',
    description: 'Actively recording meeting audio and generating transcript',
    icon: 'microphone',
    type: 'success',
    color: 'green',
  },
  COMPLETED: {
    message: 'Meeting completed successfully',
    description: 'Meeting ended normally, transcript processing complete',
    icon: 'status-positive',
    type: 'success',
    color: 'green',
  },
  FAILED: {
    message: 'Failed to join meeting',
    description: 'Check error details and troubleshooting steps below',
    icon: 'status-negative',
    type: 'error',
    color: 'red',
  },
  ENDED: {
    message: 'Virtual participant ended by user',
    description: 'Manually terminated by user action',
    icon: 'status-stopped',
    type: 'stopped',
    color: 'grey',
  },
  CANCELLED: {
    message: 'Schedule cancelled by user',
    description: 'Scheduled virtual participant was cancelled before execution',
    icon: 'status-stopped',
    type: 'stopped',
    color: 'grey',
  },
  MANUAL_ACTION_REQUIRED: {
    message: 'Manual action required',
    description: 'User interaction needed in VNC viewer',
    icon: 'status-warning',
    type: 'warning',
    color: 'orange',
  },
};

export const VP_STATUS_CONFIG = STATUS_CONFIG;

export const StatusBadge = ({ status }) => {
  const config = STATUS_CONFIG[status] || STATUS_CONFIG.FAILED;
  return <Badge color={config.color}>{status}</Badge>;
};

StatusBadge.propTypes = {
  status: PropTypes.string.isRequired,
};

export const StatusDetails = ({ status, updatedAt, scheduledFor, statusMessage }) => {
  const config = STATUS_CONFIG[status] || STATUS_CONFIG.FAILED;
  const isInProgress = [
    'INITIALIZING',
    'WAITING_FOR_CAPACITY',
    'BOOTING',
    'REGISTERING_NETWORK',
    'VNC_READY',
    'HYDRATING_PROFILE',
    'LAUNCHING_BROWSER',
    'WARMING_PROFILE',
    'CONNECTING',
    'JOINING',
  ].includes(status);

  // The VP backend writes a human-readable exit detail to errorMessage on
  // terminal states (e.g. "Asked to leave by Jeremy Feldman.") — show it
  // instead of the generic per-status default when present.
  const description = statusMessage || config.description;

  return (
    <Container>
      <SpaceBetween direction="vertical" size="s">
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
          {isInProgress ? (
            <Spinner size="normal" />
          ) : (
            <Icon name={config.icon} variant={config.type === 'error' ? 'error' : 'normal'} />
          )}
          <Box fontSize="heading-m" fontWeight="bold">
            {config.message}
          </Box>
        </div>
        <Box color="text-body-secondary">{description}</Box>
        {status === 'SCHEDULED' && scheduledFor && (
          <Box color="text-status-info" fontSize="body-m" fontWeight="bold">
            Scheduled for: {new Date(scheduledFor).toLocaleString()}
          </Box>
        )}
        <Box color="text-body-secondary" fontSize="body-s">
          Last updated: {new Date(updatedAt).toLocaleString()}
        </Box>
      </SpaceBetween>
    </Container>
  );
};

StatusDetails.propTypes = {
  status: PropTypes.string.isRequired,
  updatedAt: PropTypes.string.isRequired,
  scheduledFor: PropTypes.string,
  statusMessage: PropTypes.string,
};

StatusDetails.defaultProps = {
  scheduledFor: null,
  statusMessage: null,
};

export const ConnectionDetails = ({ vpDetails }) => {
  const calculateDuration = () => {
    if (!vpDetails.createdAt) return 'N/A';

    const start = new Date(vpDetails.createdAt);
    const end =
      vpDetails.status === 'COMPLETED' || vpDetails.status === 'ENDED' ? new Date(vpDetails.updatedAt) : new Date();

    const diffMs = end - start;
    const diffMins = Math.floor(diffMs / 60000);
    const diffHours = Math.floor(diffMins / 60);

    if (diffHours > 0) {
      return `${diffHours}h ${diffMins % 60}m`;
    }
    return `${diffMins}m`;
  };

  return (
    <ColumnLayout columns={3} variant="text-grid">
      <SpaceBetween size="xs">
        <Box color="text-label" fontWeight="bold">
          Platform
        </Box>
        <div>{vpDetails.meetingPlatform}</div>
      </SpaceBetween>

      <SpaceBetween size="xs">
        <Box color="text-label" fontWeight="bold">
          Meeting ID
        </Box>
        <div>{vpDetails.meetingId}</div>
      </SpaceBetween>

      <SpaceBetween size="xs">
        <Box color="text-label" fontWeight="bold">
          Duration
        </Box>
        <div>{calculateDuration()}</div>
      </SpaceBetween>

      <SpaceBetween size="xs">
        <Box color="text-label" fontWeight="bold">
          Created
        </Box>
        <div>{new Date(vpDetails.createdAt).toLocaleString()}</div>
      </SpaceBetween>

      <SpaceBetween size="xs">
        <Box color="text-label" fontWeight="bold">
          Owner
        </Box>
        <div>{vpDetails.owner || 'N/A'}</div>
      </SpaceBetween>

      <SpaceBetween size="xs">
        <Box color="text-label" fontWeight="bold">
          Status
        </Box>
        <StatusBadge status={vpDetails.status} />
      </SpaceBetween>
    </ColumnLayout>
  );
};

ConnectionDetails.propTypes = {
  vpDetails: PropTypes.shape({
    createdAt: PropTypes.string,
    status: PropTypes.string,
    updatedAt: PropTypes.string,
    meetingPlatform: PropTypes.string,
    meetingId: PropTypes.string,
    owner: PropTypes.string,
  }).isRequired,
};

const ErrorTroubleshooting = ({ status, errorDetails, errorMessage, vpId, vncReady, userAcknowledgedFailure }) => {
  const [acking, setAcking] = useState(false);
  const [ackError, setAckError] = useState(null);
  const [ackedLocal, setAckedLocal] = useState(false);

  if (status !== 'FAILED') return null;

  const acknowledged = !!userAcknowledgedFailure || ackedLocal;
  const showAckButton = !!vpId && !!vncReady && !acknowledged;

  const handleAcknowledge = async () => {
    setAcking(true);
    setAckError(null);
    try {
      await client.graphql({
        query: acknowledgeVPFailureMutation,
        variables: { virtualParticipantId: vpId },
      });
      setAckedLocal(true);
    } catch (e) {
      setAckError(e?.errors?.[0]?.message || e?.message || 'Failed to acknowledge');
    } finally {
      setAcking(false);
    }
  };

  // The backend writes a specific, user-facing failure reason to the
  // errorMessage field (e.g. "The meeting browser ran out of memory and
  // crashed during join."). Prefer that over the structured errorDetails
  // (which is only populated for a few categorised errors).
  const specificMessage = (errorDetails && errorDetails.errorMessage) || errorMessage || null;

  const getErrorSolution = () => {
    // Use enhanced error details if available
    if (errorDetails && errorDetails.troubleshootingSteps) {
      return (
        <SpaceBetween direction="vertical" size="s">
          <div>
            <strong>Recommended solutions:</strong>
          </div>
          <ul>
            {errorDetails.troubleshootingSteps.map((step) => (
              <li key={step}>{step}</li>
            ))}
          </ul>
          {errorDetails.errorCategory && (
            <div>
              <strong>Error Category:</strong> {errorDetails.errorCategory.replace(/_/g, ' ')}
            </div>
          )}
          {errorDetails.errorCode && (
            <div>
              <strong>Error Code:</strong> {errorDetails.errorCode}
            </div>
          )}
        </SpaceBetween>
      );
    }

    // When the backend gave us a specific reason, that's shown above as the
    // "Error:" line — don't bury it under the generic checklist, which would
    // be misleading (e.g. an out-of-memory crash has nothing to do with the
    // meeting ID or password).
    if (specificMessage) {
      return null;
    }

    // Fallback to generic solutions only when we have no specific reason.
    return (
      <SpaceBetween direction="vertical" size="s">
        <div>
          <strong>Common solutions:</strong>
        </div>
        <ul>
          <li>Verify the meeting ID is correct and the meeting has started</li>
          <li>Check if a meeting password is required</li>
          <li>Ensure the meeting hasn&apos;t ended or been cancelled</li>
          <li>Verify you have permission to join this meeting</li>
        </ul>
      </SpaceBetween>
    );
  };

  return (
    <Container header={<Header variant="h3">Troubleshooting</Header>}>
      <SpaceBetween direction="vertical" size="s">
        <Alert type="error">
          <SpaceBetween direction="vertical" size="s">
            <div>
              <strong>Virtual Participant failed to join the meeting</strong>
            </div>
            {specificMessage && (
              <div>
                <strong>Error:</strong> {specificMessage}
              </div>
            )}
            {getErrorSolution()}
          </SpaceBetween>
        </Alert>
        {showAckButton && (
          <Alert type="info">
            <SpaceBetween direction="vertical" size="s">
              <div>
                The live browser above is still available so you can inspect what tripped up the join (e.g. a CAPTCHA,
                an unexpected dialog, or a stuck page).
              </div>
              <div>
                Click <strong>Got it — close Virtual Participant session</strong> when you&apos;re done. The session
                will close automatically after 10 minutes.
              </div>
              {ackError && <div style={{ color: '#d13212' }}>{ackError}</div>}
              <Button onClick={handleAcknowledge} loading={acking} variant="primary">
                Got it — close Virtual Participant session
              </Button>
            </SpaceBetween>
          </Alert>
        )}
        {acknowledged && vncReady && (
          <Alert type="success">Acknowledged. Virtual Participant session is closing.</Alert>
        )}
      </SpaceBetween>
    </Container>
  );
};

ErrorTroubleshooting.propTypes = {
  status: PropTypes.string.isRequired,
  errorDetails: PropTypes.shape({
    errorCode: PropTypes.string,
    errorMessage: PropTypes.string,
    errorCategory: PropTypes.string,
    troubleshootingSteps: PropTypes.arrayOf(PropTypes.string),
    lastErrorAt: PropTypes.string,
    errorCount: PropTypes.number,
  }),
  errorMessage: PropTypes.string,
  vpId: PropTypes.string,
  vncReady: PropTypes.bool,
  userAcknowledgedFailure: PropTypes.bool,
};

ErrorTroubleshooting.defaultProps = {
  errorDetails: null,
  errorMessage: null,
  vpId: null,
  vncReady: false,
  userAcknowledgedFailure: false,
};

const ActionButtons = ({ vpDetails, onRefresh, onEnd, onCancelSchedule }) => {
  const canEnd = ['JOINING', 'JOINED', 'ACTIVE'].includes(vpDetails.status);
  const canCancelSchedule = vpDetails.status === 'SCHEDULED' && vpDetails.isScheduled;

  return (
    <Container header={<Header variant="h3">Actions</Header>}>
      <SpaceBetween direction="horizontal" size="s">
        <Button iconName="refresh" onClick={onRefresh}>
          Refresh Status
        </Button>

        {canEnd && (
          <Button variant="normal" iconName="close" onClick={onEnd}>
            End Virtual Participant
          </Button>
        )}

        {canCancelSchedule && (
          <Button variant="normal" iconName="close" onClick={onCancelSchedule}>
            Cancel Schedule
          </Button>
        )}

        {vpDetails.CallId && (
          <RouterLink to={`/calls/${encodeURIComponent(vpDetails.CallId)}`} style={{ textDecoration: 'none' }}>
            <Button iconName="external">View Meeting Transcript</Button>
          </RouterLink>
        )}

        <RouterLink to="/calls" style={{ textDecoration: 'none' }}>
          <Button iconName="external">View All Meetings</Button>
        </RouterLink>
      </SpaceBetween>
    </Container>
  );
};

ActionButtons.propTypes = {
  vpDetails: PropTypes.shape({
    status: PropTypes.string.isRequired,
    CallId: PropTypes.string,
    isScheduled: PropTypes.bool,
  }).isRequired,
  onRefresh: PropTypes.func.isRequired,
  onEnd: PropTypes.func.isRequired,
  onCancelSchedule: PropTypes.func.isRequired,
};

const VirtualParticipantDetails = () => {
  const { vpId } = useParams();
  const navigate = useNavigate();
  const { authState } = useAppContext();
  const [vpDetails, setVpDetails] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [notifications, setNotifications] = useState([]);

  const loadVpDetails = async () => {
    try {
      setLoading(true);
      setError(null);

      const result = await client.graphql({ query: getVirtualParticipant, variables: { id: vpId } });

      if (result.data.getVirtualParticipant) {
        const vpData = result.data.getVirtualParticipant;
        console.log('VP Details loaded:', vpData);
        setVpDetails(vpData);
      } else {
        setError('Virtual Participant not found');
      }
    } catch (err) {
      logger.error('Error loading VP details:', err);
      setError('Failed to load Virtual Participant details');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (vpId && authState === 'authenticated') {
      loadVpDetails();
    }
  }, [vpId, authState]);

  // Set up real-time updates: AppSync subscription + retry-on-error +
  // polling fallback. The subscription is the fast path; the poll is a
  // 5s safety net that runs while the VP is still in any in-progress
  // state (so a dropped websocket / expired auth token doesn't leave
  // the page stuck on stale state until the user manually refreshes).
  useEffect(() => {
    if (!vpId) return undefined;
    if (authState !== 'authenticated') return undefined;

    let cancelled = false;
    let subscription = null;
    let pollTimer = null;
    let retryTimer = null;
    let retryAttempt = 0;

    const pickNew = (next, prev) => (next != null ? next : prev);
    const applyUpdate = (updated) => {
      if (!updated || updated.id !== vpId) return;
      setVpDetails((prev) => ({
        ...prev,
        ...updated,
        // pickNew so a partial update with status=null doesn't blank it
        status: pickNew(updated.status, prev?.status),
        updatedAt: pickNew(updated.updatedAt, prev?.updatedAt),
        CallId: pickNew(updated.CallId, prev?.CallId),
        vncEndpoint: pickNew(updated.vncEndpoint, prev?.vncEndpoint),
        vncPort: pickNew(updated.vncPort, prev?.vncPort),
        // Latch vncReady=true so a later partial update can't unset it.
        vncReady: prev?.vncReady === true ? true : pickNew(updated.vncReady, prev?.vncReady),
        manualActionType: pickNew(updated.manualActionType, prev?.manualActionType),
        manualActionMessage: pickNew(updated.manualActionMessage, prev?.manualActionMessage),
        manualActionTimeoutSeconds: pickNew(updated.manualActionTimeoutSeconds, prev?.manualActionTimeoutSeconds),
        manualActionStartTime: pickNew(updated.manualActionStartTime, prev?.manualActionStartTime),
      }));
    };

    // Quiet poll fallback: only runs while we genuinely need it (status
    // is in-progress AND vncReady is not yet true). Does NOT touch
    // loading state, so the page never flickers — uses the same
    // applyUpdate as the subscription so we merge fields rather than
    // wholesale-replacing state. Once vncReady=true is latched, polling
    // stops; the subscription remains the only update mechanism.
    const POLL_INTERVAL_MS = 5000;
    const startPolling = () => {
      if (pollTimer || cancelled) return;
      const tick = async () => {
        pollTimer = null;
        if (cancelled) return;
        // Decide whether we still need to poll, based on freshest state.
        let shouldKeepPolling = true;
        setVpDetails((prev) => {
          if (prev?.vncReady === true) shouldKeepPolling = false;
          if (prev?.status && ['COMPLETED', 'FAILED', 'ENDED', 'CANCELLED'].includes(prev.status)) {
            shouldKeepPolling = false;
          }
          return prev;
        });
        if (!shouldKeepPolling) return;
        // Quiet refetch — no setLoading, no full-replace, no notifications.
        try {
          const r = await client.graphql({
            query: getVirtualParticipant,
            variables: { id: vpId },
          });
          const fresh = r?.data?.getVirtualParticipant;
          if (fresh && !cancelled) applyUpdate(fresh);
        } catch (_) {
          // ignore — keep polling
        }
        if (!cancelled) {
          pollTimer = setTimeout(tick, POLL_INTERVAL_MS);
        }
      };
      pollTimer = setTimeout(tick, POLL_INTERVAL_MS);
    };

    const subscribe = () => {
      if (cancelled) return;
      console.log('=== Setting up AppSync subscription for VP:', vpId);
      subscription = client.graphql({ query: onUpdateVirtualParticipantDetailed }).subscribe({
        next: (message) => {
          retryAttempt = 0; // healthy subscription — reset backoff
          const updated = message?.data?.onUpdateVirtualParticipant;
          applyUpdate(updated);
          if (updated?.vncReady && updated?.vncEndpoint) {
            console.log('✓ VNC is ready! Endpoint:', updated.vncEndpoint);
          }
        },
        error: (err) => {
          console.error('=== AppSync subscription error ===', err);
          logger.error('Subscription error:', err);
          // Reconnect with exponential backoff (1s, 2s, 4s, … capped at 30s).
          // Polling fallback below keeps the UI fresh while we wait.
          if (cancelled) return;
          const delay = Math.min(30_000, 1000 * 2 ** retryAttempt);
          retryAttempt += 1;
          console.log(`Retrying subscription in ${delay}ms (attempt ${retryAttempt})`);
          retryTimer = setTimeout(() => {
            try {
              subscription?.unsubscribe?.();
            } catch {
              /* ignore */
            }
            subscribe();
          }, delay);
        },
      });
    };

    subscribe();
    startPolling();

    return () => {
      console.log('=== Unsubscribing from AppSync for VP:', vpId);
      cancelled = true;
      try {
        subscription?.unsubscribe?.();
      } catch {
        /* ignore */
      }
      if (pollTimer) clearTimeout(pollTimer);
      if (retryTimer) clearTimeout(retryTimer);
    };
  }, [vpId, authState]);

  const handleRefresh = () => {
    loadVpDetails();
  };

  const handleEnd = async () => {
    try {
      console.log('=== FRONTEND: CALLING END VP MUTATION ===');
      console.log('VP ID:', vpId);
      console.log('Mutation:', endVirtualParticipant);
      const result = await client.graphql({
        query: endVirtualParticipant,
        variables: {
          input: {
            id: vpId,
            endReason: 'User requested termination',
            endedBy: 'User',
          },
        },
      });
      console.log('=== FRONTEND: END VP MUTATION RESULT ===');
      console.log('Result:', JSON.stringify(result, null, 2));

      const notification = {
        type: 'success',
        content: 'Virtual Participant ended successfully',
        dismissible: true,
        id: `end-success-${Date.now()}`,
      };
      setNotifications((prev) => [...prev, notification]);

      // Refresh the data to show updated status
      loadVpDetails();
    } catch (err) {
      logger.error('Error ending VP:', err);
      const notification = {
        type: 'error',
        content: 'Failed to end Virtual Participant. Please try again.',
        dismissible: true,
        id: `end-error-${Date.now()}`,
      };
      setNotifications((prev) => [...prev, notification]);
    }
  };

  const handleCancelSchedule = async () => {
    try {
      console.log('=== FRONTEND: CALLING CANCEL SCHEDULE ===');
      console.log('VP ID:', vpId);
      // Use the endVirtualParticipant mutation with a different reason for scheduled VPs
      const result = await client.graphql({
        query: endVirtualParticipant,
        variables: {
          input: {
            id: vpId,
            endReason: 'Schedule cancelled by user',
            endedBy: 'User',
          },
        },
      });
      console.log('=== FRONTEND: CANCEL SCHEDULE RESULT ===');
      console.log('Result:', JSON.stringify(result, null, 2));

      const notification = {
        type: 'success',
        content: 'Virtual Participant schedule cancelled successfully',
        dismissible: true,
        id: `cancel-success-${Date.now()}`,
      };
      setNotifications((prev) => [...prev, notification]);

      // Refresh the data to show updated status
      loadVpDetails();
    } catch (err) {
      logger.error('Error cancelling VP schedule:', err);
      const notification = {
        type: 'error',
        content: 'Failed to cancel Virtual Participant schedule. Please try again.',
        dismissible: true,
        id: `cancel-error-${Date.now()}`,
      };
      setNotifications((prev) => [...prev, notification]);
    }
  };

  if (loading) {
    return (
      <Container>
        <Box textAlign="center" padding="xxl">
          <Spinner size="large" />
          <Box margin={{ top: 's' }}>Loading Virtual Participant details...</Box>
        </Box>
      </Container>
    );
  }

  if (error) {
    return (
      <Container>
        <Alert type="error">
          <SpaceBetween direction="vertical" size="s">
            <div>{error}</div>
            <Button onClick={() => navigate(-1)}>Go Back</Button>
          </SpaceBetween>
        </Alert>
      </Container>
    );
  }

  if (!vpDetails) {
    return (
      <Container>
        <Alert type="warning">Virtual Participant not found</Alert>
      </Container>
    );
  }

  return (
    <SpaceBetween direction="vertical" size="l">
      {notifications.length > 0 && <Flashbar items={notifications} />}

      {/* Header */}
      <Container>
        <Header
          variant="h1"
          actions={
            <SpaceBetween direction="horizontal" size="s">
              <Button iconName="arrow-left" onClick={() => navigate(-1)}>
                Back to List
              </Button>
              {vpDetails.CallId ? (
                <RouterLink to={`/calls/${encodeURIComponent(vpDetails.CallId)}`} style={{ textDecoration: 'none' }}>
                  <Button iconName="external">View Call Details</Button>
                </RouterLink>
              ) : (
                <Button iconName="external" disabled>
                  Call Details (Not Available)
                </Button>
              )}
            </SpaceBetween>
          }
        >
          {vpDetails.meetingName}
        </Header>
      </Container>

      {/* Current Status. The backend reuses the errorMessage field as a
          generic status-detail channel: a human-readable exit reason on
          COMPLETED, and live progress sub-steps during the long in-progress
          phases (e.g. "Waiting to be admitted…" within JOINING) so the VP
          doesn't look frozen. Surface it for both; on FAILED it's the real
          error and is shown in the troubleshooting card instead. */}
      <StatusDetails
        status={vpDetails.status}
        updatedAt={vpDetails.updatedAt}
        scheduledFor={vpDetails.scheduledFor}
        statusMessage={vpDetails.status === 'FAILED' ? null : vpDetails.errorMessage}
      />

      {/* Status Timeline - Only show if enhanced data available */}
      {vpDetails.statusHistory && (
        <StatusTimeline
          history={vpDetails.statusHistory}
          currentStatus={vpDetails.status}
          currentTimestamp={vpDetails.updatedAt}
          currentStatusMessage={vpDetails.status === 'FAILED' ? null : vpDetails.errorMessage}
        />
      )}

      {/* Connection Details */}
      <Container header={<Header variant="h3">Connection Details</Header>}>
        <ConnectionDetails vpDetails={vpDetails} />
      </Container>

      {/* VNC Live View - Show when VNC is ready and VP is active.
          FAILED is intentionally included: the VP container waits up to
          10 min after FAILED before tearing down the ALB target so the
          user can use the live browser to inspect what blocked the join. */}
      {vpDetails.vncReady &&
        vpDetails.vncEndpoint &&
        [
          'VNC_READY',
          'HYDRATING_PROFILE',
          'LAUNCHING_BROWSER',
          'CONNECTING',
          'JOINING',
          'JOINED',
          'ACTIVE',
          'MANUAL_ACTION_REQUIRED',
          'FAILED',
        ].includes(vpDetails.status) && (
          <VNCViewer
            vpId={vpId}
            vncEndpoint={vpDetails.vncEndpoint}
            websocketUrl={vpDetails.vncEndpoint}
            status={vpDetails.status}
            manualActionType={vpDetails.manualActionType}
            manualActionMessage={vpDetails.manualActionMessage}
            manualActionTimeoutSeconds={vpDetails.manualActionTimeoutSeconds}
            manualActionStartTime={vpDetails.manualActionStartTime}
          />
        )}

      {/* VNC Preparing Message - Show while VNC is starting up. Headline +
          subtext are pulled from STATUS_CONFIG so as the VP progresses
          through INITIALIZING → REGISTERING_NETWORK → HYDRATING_PROFILE →
          LAUNCHING_BROWSER → CONNECTING → JOINING, the user sees what's
          actually happening rather than a static 'Preparing...' spinner. */}
      {!vpDetails.vncReady &&
        [
          'INITIALIZING',
          'WAITING_FOR_CAPACITY',
          'BOOTING',
          'REGISTERING_NETWORK',
          'HYDRATING_PROFILE',
          'LAUNCHING_BROWSER',
          'WARMING_PROFILE',
          'CONNECTING',
          'JOINING',
        ].includes(vpDetails.status) && (
          <Container>
            <Box textAlign="center" padding="l">
              <Spinner size="large" />
              <Box margin={{ top: 's' }}>
                <strong>{STATUS_CONFIG[vpDetails.status]?.message || 'Preparing live view…'}</strong>
              </Box>
              <Box margin={{ top: 'xs' }} color="text-body-secondary">
                {STATUS_CONFIG[vpDetails.status]?.description ||
                  'VNC viewer is waiting for the VP to start up. This may take ~60 seconds.'}
              </Box>
            </Box>
          </Container>
        )}

      {/* Error Troubleshooting - Only show for failed status */}
      <ErrorTroubleshooting
        status={vpDetails.status}
        errorDetails={vpDetails.errorDetails}
        errorMessage={vpDetails.errorMessage}
        vpId={vpId}
        vncReady={vpDetails.vncReady}
        userAcknowledgedFailure={vpDetails.userAcknowledgedFailure}
      />

      {/* Basic Status Timeline for basic schema */}
      {!vpDetails.statusHistory && (
        <Container header={<Header variant="h3">Status Information</Header>}>
          <Alert type="info">
            <SpaceBetween direction="vertical" size="s">
              <div>
                <strong>Current Status:</strong> {vpDetails.status}
              </div>
              <div>
                <strong>Last Updated:</strong> {new Date(vpDetails.updatedAt).toLocaleString()}
              </div>
              <div>
                <strong>Created:</strong> {new Date(vpDetails.createdAt).toLocaleString()}
              </div>
            </SpaceBetween>
          </Alert>
        </Container>
      )}

      {/* Action Buttons */}
      <ActionButtons
        vpDetails={vpDetails}
        onRefresh={handleRefresh}
        onEnd={handleEnd}
        onCancelSchedule={handleCancelSchedule}
      />
    </SpaceBetween>
  );
};

export default VirtualParticipantDetails;
