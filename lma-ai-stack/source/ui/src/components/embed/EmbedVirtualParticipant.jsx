/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 *
 * EmbedVirtualParticipant - Embeddable virtual participant component.
 * Shows VNC live view, transcript, summary, and/or chat for a VP session.
 *
 * Query params:
 *   vpId    - Virtual participant ID to load
 *   callId  - Optional: directly specify the call ID for transcript/summary/chat
 *   show    - Comma-separated panels: vnc, transcript, summary, chat, details
 *   layout  - Layout: vertical, horizontal, grid
 */
import React, { useEffect, useState } from 'react';
import PropTypes from 'prop-types';
import { API, graphqlOperation, Logger } from 'aws-amplify';
import { Box, Container, Header, SpaceBetween, Spinner, Alert, Badge, ColumnLayout } from '@awsui/components-react';

import VNCViewer from '../virtual-participant-layout/VNCViewer';
import useSettingsContext from '../../contexts/settings';
import { CallsContext } from '../../contexts/calls';
import useCallsGraphQlApi from '../../hooks/use-calls-graphql-api';
import mapCallsAttributes from '../common/map-call-attributes';
import { IN_PROGRESS_STATUS } from '../common/get-recording-status';
import { CallPanel } from '../call-panel/CallPanel';

const logger = new Logger('EmbedVirtualParticipant');

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
      createdAt
      updatedAt
      owner
      Owner
      SharedWith
      CallId
      vncEndpoint
      vncPort
      vncReady
    }
  }
`;

const onUpdateVirtualParticipantDetailed = `
  subscription OnUpdateVirtualParticipant {
    onUpdateVirtualParticipant {
      id
      status
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
    }
  }
`;

/**
 * Compact VP status display for embed mode.
 */
const VPStatusBadge = ({ status }) => {
  const colorMap = {
    SCHEDULED: 'blue',
    INITIALIZING: 'blue',
    CONNECTING: 'blue',
    JOINING: 'blue',
    JOINED: 'green',
    ACTIVE: 'green',
    COMPLETED: 'green',
    FAILED: 'red',
    ENDED: 'grey',
    CANCELLED: 'grey',
    MANUAL_ACTION_REQUIRED: 'red',
  };
  return <Badge color={colorMap[status] || 'grey'}>{status}</Badge>;
};

VPStatusBadge.propTypes = {
  status: PropTypes.string.isRequired,
};

/**
 * VP Details panel showing connection info.
 */
const VPDetailsPanel = ({ vpDetails }) => (
  <Container header={<Header variant="h3">Virtual Participant Details</Header>}>
    <ColumnLayout columns={3} variant="text-grid">
      <SpaceBetween size="xs">
        <Box color="text-label" fontWeight="bold">
          Meeting
        </Box>
        <div>{vpDetails.meetingName}</div>
      </SpaceBetween>
      <SpaceBetween size="xs">
        <Box color="text-label" fontWeight="bold">
          Platform
        </Box>
        <div>{vpDetails.meetingPlatform}</div>
      </SpaceBetween>
      <SpaceBetween size="xs">
        <Box color="text-label" fontWeight="bold">
          Status
        </Box>
        <VPStatusBadge status={vpDetails.status} />
      </SpaceBetween>
      <SpaceBetween size="xs">
        <Box color="text-label" fontWeight="bold">
          Meeting ID
        </Box>
        <div>{vpDetails.meetingId}</div>
      </SpaceBetween>
      <SpaceBetween size="xs">
        <Box color="text-label" fontWeight="bold">
          Owner
        </Box>
        <div>{vpDetails.owner || 'N/A'}</div>
      </SpaceBetween>
      <SpaceBetween size="xs">
        <Box color="text-label" fontWeight="bold">
          Last Updated
        </Box>
        <div>{new Date(vpDetails.updatedAt).toLocaleString()}</div>
      </SpaceBetween>
    </ColumnLayout>
  </Container>
);

VPDetailsPanel.propTypes = {
  vpDetails: PropTypes.shape({
    meetingName: PropTypes.string,
    meetingPlatform: PropTypes.string,
    meetingId: PropTypes.string,
    status: PropTypes.string,
    owner: PropTypes.string,
    updatedAt: PropTypes.string,
  }).isRequired,
};

const EmbedVirtualParticipant = ({ params, sendToParent }) => {
  const { vpId, callId: paramCallId, show, layout } = params;
  const { settings } = useSettingsContext();

  const [vpDetails, setVpDetails] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  // Call details state (for transcript/summary/chat panels)
  const [call, setCall] = useState(null);
  const [callLoading, setCallLoading] = useState(false);

  const {
    calls,
    callTranscriptPerCallId,
    getCallDetailsFromCallIds,
    sendGetTranscriptSegmentsRequest,
    setLiveTranscriptCallId,
    isCallsListLoading,
    setIsCallsListLoading,
    setPeriodsToLoad,
    periodsToLoad,
  } = useCallsGraphQlApi({ initialPeriodsToLoad: 0.5 });

  const [toolsOpen, setToolsOpen] = useState(false);
  const [selectedItems, setSelectedItems] = useState([]);

  // Determine which panels to show
  const showVNC = show.includes('vnc');
  const showTranscript = show.includes('transcript');
  const showSummary = show.includes('summary');
  const showChat = show.includes('chat');
  const showDetails = show.includes('details');
  const showCallPanels = showTranscript || showSummary || showChat;

  // Load VP details
  useEffect(() => {
    if (!vpId) {
      setError('No vpId provided. Use ?vpId=<virtual-participant-id> to specify a VP.');
      setLoading(false);
      return;
    }

    const loadVP = async () => {
      try {
        setLoading(true);
        const result = await API.graphql(graphqlOperation(getVirtualParticipant, { id: vpId }));

        if (result.data.getVirtualParticipant) {
          const vpData = result.data.getVirtualParticipant;
          setVpDetails(vpData);

          sendToParent({
            type: 'LMA_VP_LOADED',
            vpId,
            status: vpData.status,
            callId: vpData.CallId,
          });
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

    loadVP();
  }, [vpId]);

  // Subscribe to VP updates
  useEffect(() => {
    if (!vpId) return undefined;

    const subscription = API.graphql(graphqlOperation(onUpdateVirtualParticipantDetailed)).subscribe({
      next: ({ value }) => {
        const updated = value?.data?.onUpdateVirtualParticipant;
        if (updated && updated.id === vpId) {
          setVpDetails((prev) => ({
            ...prev,
            status: updated.status,
            updatedAt: updated.updatedAt,
            CallId: updated.CallId || prev?.CallId,
            vncEndpoint: updated.vncEndpoint || prev?.vncEndpoint,
            vncPort: updated.vncPort || prev?.vncPort,
            vncReady: updated.vncReady !== undefined ? updated.vncReady : prev?.vncReady,
            manualActionType: updated.manualActionType || prev?.manualActionType,
            manualActionMessage: updated.manualActionMessage || prev?.manualActionMessage,
            manualActionTimeoutSeconds: updated.manualActionTimeoutSeconds || prev?.manualActionTimeoutSeconds,
            manualActionStartTime: updated.manualActionStartTime || prev?.manualActionStartTime,
          }));

          sendToParent({
            type: 'LMA_VP_STATUS_CHANGED',
            vpId,
            status: updated.status,
            callId: updated.CallId,
          });
        }
      },
      error: (err) => logger.error('VP subscription error:', err),
    });

    return () => subscription.unsubscribe();
  }, [vpId]);

  // Load call details when VP has a CallId and we need call panels
  const effectiveCallId = paramCallId || vpDetails?.CallId;

  useEffect(() => {
    if (!effectiveCallId || !showCallPanels) return () => {};

    const fetchCall = async () => {
      try {
        setCallLoading(true);
        const response = await getCallDetailsFromCallIds([effectiveCallId]);
        const callsMap = mapCallsAttributes(response, settings);
        const callDetails = callsMap[0];

        if (callDetails) {
          setCall(callDetails);
          if (!callTranscriptPerCallId[effectiveCallId]) {
            await sendGetTranscriptSegmentsRequest(effectiveCallId);
          }
          if (callDetails?.recordingStatusLabel === IN_PROGRESS_STATUS) {
            setLiveTranscriptCallId(effectiveCallId);
          }
        }
      } catch (err) {
        logger.error('Error fetching call details for VP:', err);
      } finally {
        setCallLoading(false);
      }
    };

    fetchCall();
    return () => setLiveTranscriptCallId(null);
  }, [effectiveCallId, showCallPanels]);

  // Update call from real-time updates
  useEffect(() => {
    if (!effectiveCallId || !call || !calls?.length) return;

    const callsFiltered = calls.filter((c) => c.CallId === effectiveCallId);
    if (callsFiltered?.length) {
      const callsMap = mapCallsAttributes([callsFiltered[0]], settings);
      const callDetails = callsMap[0];
      if (callDetails?.updatedAt && call.updatedAt < callDetails.updatedAt) {
        setCall(callDetails);
      }
    }
  }, [calls, effectiveCallId]);

  // Build calls context
  // eslint-disable-next-line react/jsx-no-constructed-context-values
  const callsContextValue = {
    calls,
    callTranscriptPerCallId,
    getCallDetailsFromCallIds,
    isCallsListLoading,
    selectedItems,
    sendGetTranscriptSegmentsRequest,
    setIsCallsListLoading,
    setLiveTranscriptCallId,
    setPeriodsToLoad,
    setToolsOpen,
    setSelectedItems,
    periodsToLoad,
    toolsOpen,
  };

  if (loading) {
    return (
      <Box textAlign="center" padding="xxl">
        <Spinner size="large" />
        <Box margin={{ top: 's' }}>Loading Virtual Participant...</Box>
      </Box>
    );
  }

  if (error) {
    return (
      <Box padding="l">
        <Alert type="error" header="Error">
          {error}
        </Alert>
      </Box>
    );
  }

  if (!vpDetails) {
    return (
      <Box padding="l">
        <Alert type="warning">Virtual Participant not found</Alert>
      </Box>
    );
  }

  const isVNCActive =
    vpDetails.vncReady &&
    vpDetails.vncEndpoint &&
    ['VNC_READY', 'CONNECTING', 'JOINING', 'JOINED', 'ACTIVE', 'MANUAL_ACTION_REQUIRED'].includes(vpDetails.status);

  const isVNCPreparing = !vpDetails.vncReady && ['INITIALIZING', 'CONNECTING', 'JOINING'].includes(vpDetails.status);

  return (
    <CallsContext.Provider value={callsContextValue}>
      <div className={`embed-layout-${layout}`}>
        {/* VP Details panel */}
        {showDetails && (
          <div className="embed-panel">
            <VPDetailsPanel vpDetails={vpDetails} />
          </div>
        )}

        {/* VNC Live View */}
        {showVNC && isVNCActive && (
          <div className="embed-panel embed-vnc-panel">
            <VNCViewer
              vpId={vpId}
              vncEndpoint={vpDetails.vncEndpoint}
              websocketUrl={vpDetails.vncEndpoint}
              status={vpDetails.status}
              manualActionType={vpDetails.manualActionType}
              manualActionMessage={vpDetails.manualActionMessage}
              manualActionTimeoutSeconds={vpDetails.manualActionTimeoutSeconds}
              manualActionStartTime={vpDetails.manualActionStartTime}
              showHeader
            />
          </div>
        )}

        {/* VNC Preparing state */}
        {showVNC && isVNCPreparing && (
          <div className="embed-panel">
            <Container>
              <Box textAlign="center" padding="l">
                <Spinner size="large" />
                <Box margin={{ top: 's' }}>
                  <strong>Preparing live view...</strong>
                </Box>
                <Box margin={{ top: 'xs' }} color="text-body-secondary">
                  VNC viewer is waiting for the VP to start up. This may take ~60 seconds.
                </Box>
              </Box>
            </Container>
          </div>
        )}

        {/* VNC not available */}
        {showVNC && !isVNCActive && !isVNCPreparing && (
          <div className="embed-panel">
            <Container header={<Header variant="h3">Live View</Header>}>
              <Box textAlign="center" padding="l" color="text-body-secondary">
                Live view is not available. VP status: <VPStatusBadge status={vpDetails.status} />
              </Box>
            </Container>
          </div>
        )}

        {/* Call panels (transcript, summary, chat) */}
        {showCallPanels && call && (
          <div className="embed-panel">
            <CallPanel
              item={call}
              setToolsOpen={setToolsOpen}
              callTranscriptPerCallId={callTranscriptPerCallId}
              getCallDetailsFromCallIds={getCallDetailsFromCallIds}
            />
          </div>
        )}

        {/* Call panels loading */}
        {showCallPanels && !call && callLoading && (
          <div className="embed-panel">
            <Box textAlign="center" padding="l">
              <Spinner size="normal" />
              <Box margin={{ top: 'xs' }}>Loading meeting transcript...</Box>
            </Box>
          </div>
        )}

        {/* Call panels - no call ID yet */}
        {showCallPanels && !call && !callLoading && !effectiveCallId && (
          <div className="embed-panel">
            <Container>
              <Box textAlign="center" padding="l" color="text-body-secondary">
                Meeting transcript will appear once the virtual participant joins and starts recording.
              </Box>
            </Container>
          </div>
        )}
      </div>
    </CallsContext.Provider>
  );
};

EmbedVirtualParticipant.propTypes = {
  params: PropTypes.shape({
    vpId: PropTypes.string,
    callId: PropTypes.string,
    show: PropTypes.arrayOf(PropTypes.string),
    layout: PropTypes.string,
  }).isRequired,
  sendToParent: PropTypes.func.isRequired,
};

export default EmbedVirtualParticipant;
