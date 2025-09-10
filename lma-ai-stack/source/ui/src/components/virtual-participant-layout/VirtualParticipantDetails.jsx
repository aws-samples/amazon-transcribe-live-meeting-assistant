import React, { useState, useEffect } from 'react';
import { useParams, useHistory } from 'react-router-dom';
import { API, graphqlOperation, Logger } from 'aws-amplify';
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
} from '@awsui/components-react';
import {
  getVirtualParticipant,
  onUpdateVirtualParticipantDetailed,
  endVirtualParticipant,
} from '../../graphql/queries/virtualParticipantQueries';
import StatusTimeline from './StatusTimeline';
import PerformanceMetrics from './PerformanceMetrics';

const logger = new Logger('VirtualParticipantDetails');

// Status configuration with enhanced messaging
const STATUS_CONFIG = {
  INITIALIZING: {
    message: 'Setting up virtual participant...',
    description: 'Preparing connection parameters and authentication',
    icon: 'loading',
    type: 'in-progress',
    color: 'blue',
  },
  CONNECTING: {
    message: 'Connecting to meeting platform...',
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
};

const StatusBadge = ({ status }) => {
  const config = STATUS_CONFIG[status] || STATUS_CONFIG.FAILED;
  return <Badge color={config.color}>{status}</Badge>;
};

StatusBadge.propTypes = {
  status: PropTypes.string.isRequired,
};

const StatusDetails = ({ status, updatedAt }) => {
  const config = STATUS_CONFIG[status] || STATUS_CONFIG.FAILED;

  return (
    <Container>
      <SpaceBetween direction="vertical" size="s">
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
          <Icon name={config.icon} variant={config.type === 'error' ? 'error' : 'normal'} />
          <Box fontSize="heading-m" fontWeight="bold">
            {config.message}
          </Box>
        </div>
        <Box color="text-body-secondary">{config.description}</Box>
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
};

const ConnectionDetails = ({ vpDetails }) => {
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

const ErrorTroubleshooting = ({ status, errorDetails }) => {
  if (status !== 'FAILED') return null;

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

    // Fallback to generic solutions
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
      <Alert type="error">
        <SpaceBetween direction="vertical" size="s">
          <div>
            <strong>Virtual Participant failed to join the meeting</strong>
          </div>
          {errorDetails && errorDetails.errorMessage && (
            <div>
              <strong>Error:</strong> {errorDetails.errorMessage}
            </div>
          )}
          {getErrorSolution()}
        </SpaceBetween>
      </Alert>
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
};

ErrorTroubleshooting.defaultProps = {
  errorDetails: null,
};

const ActionButtons = ({ vpDetails, onRefresh, onEnd }) => {
  const canEnd = ['JOINING', 'JOINED', 'ACTIVE'].includes(vpDetails.status);

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

        {vpDetails.relatedCallId && (
          <Button iconName="external" href={`#/calls/${vpDetails.relatedCallId}`}>
            View Meeting Transcript
          </Button>
        )}

        <Button iconName="external" href="#/calls">
          View All Meetings
        </Button>
      </SpaceBetween>
    </Container>
  );
};

ActionButtons.propTypes = {
  vpDetails: PropTypes.shape({
    status: PropTypes.string.isRequired,
    relatedCallId: PropTypes.string,
  }).isRequired,
  onRefresh: PropTypes.func.isRequired,
  onEnd: PropTypes.func.isRequired,
};

const VirtualParticipantDetails = () => {
  const { vpId } = useParams();
  const history = useHistory();
  const [vpDetails, setVpDetails] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [notifications, setNotifications] = useState([]);

  const loadVpDetails = async () => {
    try {
      setLoading(true);
      setError(null);

      const result = await API.graphql(graphqlOperation(getVirtualParticipant, { id: vpId }));

      if (result.data.getVirtualParticipant) {
        setVpDetails(result.data.getVirtualParticipant);
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
    if (vpId) {
      loadVpDetails();
    }
  }, [vpId, loadVpDetails]);

  // Set up real-time updates subscription
  useEffect(() => {
    if (!vpId) return undefined;

    const subscription = API.graphql(graphqlOperation(onUpdateVirtualParticipantDetailed, { id: vpId })).subscribe({
      next: ({ value }) => {
        const updated = value?.data?.onUpdateVirtualParticipant;
        if (updated && updated.id === vpId) {
          setVpDetails((prev) => ({
            ...prev,
            status: updated.status,
            updatedAt: updated.updatedAt,
          }));

          // Show notification for status changes
          const config = STATUS_CONFIG[updated.status];
          if (config) {
            const notification = {
              type: config.type === 'error' ? 'error' : 'success',
              content: config.message,
              dismissible: true,
              id: `status-${Date.now()}`,
              onDismiss: () => setNotifications((prev) => prev.filter((n) => n.id !== `status-${Date.now()}`)),
            };
            setNotifications((prev) => [...prev, notification]);
          }
        }
      },
      error: (err) => {
        logger.error('Subscription error:', err);
      },
    });

    return () => subscription.unsubscribe();
  }, [vpId]);

  const handleRefresh = () => {
    loadVpDetails();
  };

  const handleEnd = async () => {
    try {
      await API.graphql(
        graphqlOperation(endVirtualParticipant, {
          input: {
            id: vpId,
            endReason: 'User requested termination',
            endedBy: 'User',
          },
        }),
      );

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
            <Button onClick={() => history.goBack()}>Go Back</Button>
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
            <Button iconName="arrow-left" onClick={() => history.goBack()}>
              Back to List
            </Button>
          }
        >
          {vpDetails.meetingName}
        </Header>
      </Container>

      {/* Current Status */}
      <StatusDetails status={vpDetails.status} updatedAt={vpDetails.updatedAt} />

      {/* Status Timeline */}
      <StatusTimeline
        history={vpDetails.statusHistory}
        currentStatus={vpDetails.status}
        currentTimestamp={vpDetails.updatedAt}
      />

      {/* Connection Details */}
      <Container header={<Header variant="h3">Connection Details</Header>}>
        <ConnectionDetails vpDetails={vpDetails} />
      </Container>

      {/* Performance Metrics */}
      {(vpDetails.metrics || vpDetails.connectionDetails) && <PerformanceMetrics vpDetails={vpDetails} />}

      {/* Error Troubleshooting */}
      <ErrorTroubleshooting status={vpDetails.status} errorDetails={vpDetails.errorDetails} />

      {/* Action Buttons */}
      <ActionButtons vpDetails={vpDetails} onRefresh={handleRefresh} onEnd={handleEnd} />
    </SpaceBetween>
  );
};

export default VirtualParticipantDetails;
