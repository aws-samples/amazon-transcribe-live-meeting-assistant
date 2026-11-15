/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import { ConsoleLogger } from 'aws-amplify/utils';
import React, { useEffect, useState } from 'react';
import { useParams } from 'react-router-dom';
import useCallsContext from '../../contexts/calls';

import mapCallsAttributes, { adoptMeetingCopy, agendaChanged } from '../common/map-call-attributes';
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

  const [call, setCall] = useState(null);

  const sendInitCallRequests = async () => {
    const response = await getCallDetailsFromCallIds([callId]);
    logger.debug('call detail response', response);
    const callsMap = mapCallsAttributes(response);
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
      logger.debug('set live transcript meeting to null');
      setLiveTranscriptCallId(null);
    };
  }, [callId]);

  useEffect(() => {
    if (!callId || !call || !calls?.length) {
      return;
    }
    const callsFiltered = calls.filter((c) => c.CallId === callId);
    if (callsFiltered && callsFiltered?.length) {
      const callsMap = mapCallsAttributes([callsFiltered[0]]);
      const callDetails = callsMap[0];
      // UpdatedAt advancing is the usual signal that a meeting changed, but it
      // is not the only one: a speaker rename and a summary edit or
      // regeneration all deliberately leave UpdatedAt alone, so they cannot
      // interfere with the ordering guard the Kinesis pipeline's own Call
      // updates rely on. Without the further tests a rename would not reach the
      // transcript, and a regenerated summary would not reach the summary
      // panel, until the page was reloaded.
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
      const summaryChanged =
        (callDetails?.callSummaryText !== undefined && call.callSummaryText !== callDetails.callSummaryText) ||
        (callDetails?.callSummaryState !== undefined && call.callSummaryState !== callDetails.callSummaryState);
      // A meeting that started or ended is a change whatever its UpdatedAt says.
      // An update that carries no status at all ('Unknown') says nothing about it.
      const statusChanged =
        callDetails?.recordingStatusLabel !== 'Unknown' &&
        call.recordingStatusLabel !== callDetails?.recordingStatusLabel &&
        // ...unless this copy is older than the one on screen.
        !(callDetails?.updatedAt && call.updatedAt && callDetails.updatedAt < call.updatedAt);
      // An agenda write leaves UpdatedAt alone too, and is the one write that
      // carries the agenda.
      const changed = timestampAdvanced || speakerNamesChanged || summaryChanged || statusChanged;
      if (changed || agendaChanged(call, callDetails)) {
        logger.debug('Updating call', callDetails);
        // The list's copy has no agenda, capture context or retention fields
        // unless an update carried them; the page keeps its own (adoptMeetingCopy).
        setCall(adoptMeetingCopy(call, callDetails));
      }
    }
  }, [calls, callId]);

  /**
   * Read the meeting again, for a part of the page that has learned the copy it
   * holds is stale: the Speakers panel, when the backend says the meeting has
   * ended while this page still shows it in progress. That happens when the
   * page's subscription dropped -- a tab left in the background, a laptop that
   * slept -- and missed the update that ended it.
   */
  const refreshCall = async () => {
    const [fresh] = mapCallsAttributes(await getCallDetailsFromCallIds([callId]));
    if (fresh) setCall(fresh);
  };

  return (
    call && (
      <CallPanel
        item={call}
        setToolsOpen={setToolsOpen}
        callTranscriptPerCallId={callTranscriptPerCallId}
        getCallDetailsFromCallIds={getCallDetailsFromCallIds}
        onRefreshMeeting={refreshCall}
        // The meeting's own page keeps the selected tab in the URL (?tab=...).
        tabInUrl
      />
    )
  );
};

export default CallDetails;
