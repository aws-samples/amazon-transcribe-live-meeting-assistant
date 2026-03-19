/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 *
 * EmbedCallDetails - Embeddable call details with selectable sub-panels.
 * Renders transcript, summary, and/or chat panels for a given callId.
 *
 * Query params:
 *   callId  - The meeting/call ID to load
 *   show    - Comma-separated panels: transcript, summary, chat
 *   layout  - Layout: vertical, horizontal, grid
 */
import React, { useEffect, useState } from 'react';
import PropTypes from 'prop-types';
import { Logger } from 'aws-amplify';
import { Box, Spinner, Alert } from '@awsui/components-react';

import useSettingsContext from '../../contexts/settings';
import { CallsContext } from '../../contexts/calls';
import useCallsGraphQlApi from '../../hooks/use-calls-graphql-api';
import mapCallsAttributes from '../common/map-call-attributes';
import { IN_PROGRESS_STATUS } from '../common/get-recording-status';
import { CallPanel } from '../call-panel/CallPanel';

const logger = new Logger('EmbedCallDetails');

/**
 * Standalone wrapper that provides CallsContext and renders CallPanel
 * with visibility controlled by the 'show' parameter.
 */
const EmbedCallDetails = ({ params, sendToParent }) => {
  const { callId, layout } = params;
  const { settings } = useSettingsContext();

  const [call, setCall] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

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

  // Fetch call details on mount
  useEffect(() => {
    if (!callId) {
      setError('No callId provided. Use ?callId=<meeting-id> to specify a meeting.');
      setLoading(false);
      return () => {};
    }

    const fetchCall = async () => {
      try {
        setLoading(true);
        const response = await getCallDetailsFromCallIds([callId]);
        logger.debug('Call detail response:', response);

        const callsMap = mapCallsAttributes(response, settings);
        const callDetails = callsMap[0];

        if (callDetails) {
          setCall(callDetails);
          if (!callTranscriptPerCallId[callId]) {
            await sendGetTranscriptSegmentsRequest(callId);
          }
          if (callDetails?.recordingStatusLabel === IN_PROGRESS_STATUS) {
            setLiveTranscriptCallId(callId);
          }

          // Notify parent
          sendToParent({
            type: 'LMA_CALL_LOADED',
            callId,
            status: callDetails.recordingStatusLabel,
          });
        } else {
          setError(`Meeting "${callId}" not found.`);
        }
      } catch (err) {
        logger.error('Error fetching call details:', err);
        setError('Failed to load meeting details. Please check the callId and try again.');
      } finally {
        setLoading(false);
      }
    };

    fetchCall();

    return () => {
      setLiveTranscriptCallId(null);
    };
  }, [callId]);

  // Update call when calls list updates (real-time)
  useEffect(() => {
    if (!callId || !call || !calls?.length) return;

    const callsFiltered = calls.filter((c) => c.CallId === callId);
    if (callsFiltered?.length) {
      const callsMap = mapCallsAttributes([callsFiltered[0]], settings);
      const callDetails = callsMap[0];
      if (callDetails?.updatedAt && call.updatedAt < callDetails.updatedAt) {
        setCall(callDetails);
      }
    }
  }, [calls, callId]);

  // Build calls context value
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
        <Box margin={{ top: 's' }}>Loading meeting details...</Box>
      </Box>
    );
  }

  if (error) {
    return (
      <Box padding="l">
        <Alert type="error" header="Error Loading Meeting">
          {error}
        </Alert>
      </Box>
    );
  }

  if (!call) {
    return (
      <Box padding="l">
        <Alert type="warning">Meeting not found</Alert>
      </Box>
    );
  }

  // Render the full CallPanel within CallsContext
  // The CallPanel already handles transcript, summary, chat, and VNC internally
  // We wrap it in the context it needs
  return (
    <CallsContext.Provider value={callsContextValue}>
      <div className={`embed-layout-${layout}`}>
        <CallPanel
          item={call}
          setToolsOpen={setToolsOpen}
          callTranscriptPerCallId={callTranscriptPerCallId}
          getCallDetailsFromCallIds={getCallDetailsFromCallIds}
        />
      </div>
    </CallsContext.Provider>
  );
};

EmbedCallDetails.propTypes = {
  params: PropTypes.shape({
    callId: PropTypes.string,
    show: PropTypes.arrayOf(PropTypes.string),
    layout: PropTypes.string,
  }).isRequired,
  sendToParent: PropTypes.func.isRequired,
};

export default EmbedCallDetails;
