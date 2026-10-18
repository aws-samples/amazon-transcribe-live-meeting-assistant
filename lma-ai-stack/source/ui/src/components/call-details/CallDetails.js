/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import { ConsoleLogger } from 'aws-amplify/utils';
import React, { useEffect, useState } from 'react';
import { useParams } from 'react-router-dom';
import useCallsContext from '../../contexts/calls';
import useSettingsContext from '../../contexts/settings';

import mapCallsAttributes from '../common/map-call-attributes';
import { IN_PROGRESS_STATUS } from '../common/get-recording-status';

import '@cloudscape-design/global-styles/index.css';

import CallPanel from '../call-panel';

const logger = new ConsoleLogger('CallDetails');

const CallDetails = () => {
  const { callId } = useParams();
  const {
    calls,
    callTranscriptPerCallId,
    getCallDetailsFromCallIds,
    sendGetTranscriptSegmentsRequest,
    setToolsOpen,
    setLiveTranscriptCallId,
  } = useCallsContext();
  const { settings } = useSettingsContext();

  const [call, setCall] = useState(null);

  const sendInitCallRequests = async () => {
    const response = await getCallDetailsFromCallIds([callId]);
    logger.debug('call detail response', response);
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
    }
  };

  useEffect(() => {
    if (!callId) {
      return () => {};
    }
    sendInitCallRequests();
    return () => {
      logger.debug('set live transcript contact to null');
      setLiveTranscriptCallId(null);
    };
  }, [callId]);

  useEffect(() => {
    if (!callId || !call || !calls?.length) {
      return;
    }
    const callsFiltered = calls.filter((c) => c.CallId === callId);
    if (callsFiltered && callsFiltered?.length) {
      const callsMap = mapCallsAttributes([callsFiltered[0]], settings);
      const callDetails = callsMap[0];
      // UpdatedAt advancing is the usual signal that a meeting changed, but it
      // is not the only one: a speaker rename deliberately leaves UpdatedAt
      // alone so it cannot interfere with the ordering guard the Kinesis
      // pipeline's own Call updates rely on. Without the second test a rename
      // would not reach the transcript until the page was reloaded.
      //
      // `undefined` means "this copy carries no information about the names",
      // NOT "the names were cleared. The meeting-list query deliberately omits
      // SpeakerIdentities, and a list refresh empties `calls` before
      // repopulating it, so the list-derived copy of an open meeting routinely
      // has none. Treating that as a change would replace the loaded meeting
      // with the list copy and blank every name on screen.
      const timestampAdvanced = callDetails?.updatedAt && call.updatedAt < callDetails.updatedAt;
      const speakerNamesChanged =
        callDetails?.speakerIdentities !== undefined && call.speakerIdentities !== callDetails.speakerIdentities;
      if (timestampAdvanced || speakerNamesChanged) {
        logger.debug('Updating call', callDetails);
        setCall(callDetails);
      }
    }
  }, [calls, callId]);

  return (
    call && (
      <CallPanel
        item={call}
        setToolsOpen={setToolsOpen}
        callTranscriptPerCallId={callTranscriptPerCallId}
        getCallDetailsFromCallIds={getCallDetailsFromCallIds}
      />
    )
  );
};

export default CallDetails;
