/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The read-only page of a meeting shared with a Participant.
 *
 * It is the meeting page (call-panel/CallPanel.jsx) in its Participant mode:
 * the same header, tabs, transcript (with Auto-scroll), player and meeting
 * assistant panel, with everything on it read through the Participant view
 * (getParticipantMeeting and the operations beside it), which the server
 * builds from an allowlist -- the transcript, live while the meeting is in
 * progress (onAddParticipantTranscriptSegment), the summary and its action
 * items, the agenda, the meeting chat lines, the recordings (links the server
 * signs for this meeting), the consent record and the names on the
 * participant list. This component reads those and hands them to the meeting
 * page; the meeting page in Participant mode makes no other request and
 * renders none of the owner's panels or controls.
 *
 * The meeting is read again every 30 seconds while it is in progress, which
 * also notices a meeting no longer shared with the reader: the page then says
 * so and stops the live transcript.
 */
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import PropTypes from 'prop-types';
import { useParams } from 'react-router-dom';
import { generateClient } from 'aws-amplify/api';
import { ConsoleLogger } from 'aws-amplify/utils';
import { Alert, Box, Link } from '@cloudscape-design/components';

import { CallPanel } from '../call-panel/CallPanel';
import {
  getParticipantMeeting,
  getParticipantMeetingRecordings,
  getParticipantMeetingTranscript,
  onAddParticipantTranscriptSegment,
} from '../../graphql/queries/participantMeetings';
import { SHARED_MEETINGS_PATH } from '../../routes/constants';
import { mergeSegments, mergeSegment } from './participant-transcript';
import { participantMeetingItem, participantTranscript, recordingLinks } from './participant-meeting';

const logger = new ConsoleLogger('ParticipantMeetingPage');

const REFRESH_MS = 30000;
// The most transcript pages read in one go; a meeting is a few pages at most.
const MAX_TRANSCRIPT_PAGES = 50;

const isLive = (meeting) => meeting && meeting.Status !== 'ENDED' && meeting.Status !== 'ERRORED';

const refusal = (error) => {
  const text = `${error?.message || ''} ${(error?.errors || []).map((e) => e.message).join(' ')}`;
  return /Unauthorized|not authorized|Not Authorized/i.test(text);
};

const hasRecordings = (meeting) =>
  Boolean(meeting && (meeting.HasAudioRecording || meeting.HasVideoRecording) && !meeting.RecordingsDeletedAt);

const ParticipantMeetingPage = ({
  client: givenClient = undefined,
  callId: givenCallId = undefined,
  setToolsOpen = () => {},
}) => {
  const params = useParams();
  const callId = givenCallId || params.callId;
  const clientRef = useRef(givenClient);
  if (!clientRef.current) clientRef.current = generateClient();
  const client = clientRef.current;

  const [meeting, setMeeting] = useState(null);
  const [segments, setSegments] = useState([]);
  const [links, setLinks] = useState([]);
  const [state, setState] = useState('loading');

  const readMeeting = useCallback(async () => {
    try {
      const result = await client.graphql({ query: getParticipantMeeting, variables: { CallId: callId } });
      const found = result?.data?.getParticipantMeeting;
      if (!found) {
        setState('refused');
        return null;
      }
      setMeeting(found);
      setState('ready');
      return found;
    } catch (e) {
      logger.error('getParticipantMeeting failed', e);
      setState(refusal(e) ? 'refused' : 'error');
      return null;
    }
  }, [client, callId]);

  const readTranscript = useCallback(async () => {
    let token = null;
    let collected = [];
    for (let page = 0; page < MAX_TRANSCRIPT_PAGES; page += 1) {
      // Each page needs the one before it for its token.
      // eslint-disable-next-line no-await-in-loop
      const result = await client.graphql({
        query: getParticipantMeetingTranscript,
        variables: { CallId: callId, nextToken: token },
      });
      const data = result?.data?.getParticipantMeetingTranscript || {};
      collected = mergeSegments(collected, data.Segments || []);
      token = data.nextToken;
      if (!token) break;
    }
    setSegments((current) => mergeSegments(collected, current));
  }, [client, callId]);

  useEffect(() => {
    setSegments([]);
    setLinks([]);
    setMeeting(null);
    setState('loading');
    readMeeting().then((found) => {
      if (found) readTranscript().catch((e) => logger.error('transcript read failed', e));
    });
  }, [readMeeting, readTranscript]);

  const live = state === 'ready' && isLive(meeting);

  // The live transcript, while the meeting is in progress and shared.
  useEffect(() => {
    if (!live) return undefined;
    const subscription = client
      .graphql({ query: onAddParticipantTranscriptSegment, variables: { CallId: callId } })
      .subscribe({
        next: (message) => {
          const segment = message?.data?.onAddParticipantTranscriptSegment;
          if (segment) setSegments((current) => mergeSegment(current, segment));
        },
        error: (e) => logger.error('live transcript subscription error', e),
      });
    return () => subscription?.unsubscribe?.();
  }, [client, callId, live]);

  // Read again while in progress: the status, the summary when it arrives, and
  // whether the meeting is still shared with the reader.
  useEffect(() => {
    if (!live) return undefined;
    const timer = setInterval(readMeeting, REFRESH_MS);
    return () => clearInterval(timer);
  }, [live, readMeeting]);

  // The recording links, read once the meeting has ended (nothing is played
  // while it is live). The player asks the same reader again for a fresh link
  // when one stops working, so a long listen outlives the hour each lasts.
  const recordings = useMemo(
    () =>
      recordingLinks(async () => {
        const result = await client.graphql({ query: getParticipantMeetingRecordings, variables: { CallId: callId } });
        return result?.data?.getParticipantMeetingRecordings || [];
      }),
    [client, callId],
  );
  const recordingsReadable = state === 'ready' && !live && hasRecordings(meeting);
  useEffect(() => {
    if (!recordingsReadable) return undefined;
    let current = true;
    recordings
      .load()
      .then((found) => {
        if (current) setLinks(found);
      })
      .catch((e) => logger.error('getParticipantMeetingRecordings failed', e));
    return () => {
      current = false;
    };
  }, [recordings, recordingsReadable]);

  const item = useMemo(() => (meeting ? participantMeetingItem(meeting, links) : null), [meeting, links]);
  const transcript = useMemo(() => participantTranscript(callId, segments), [callId, segments]);
  const participantMeeting = useMemo(
    () => ({
      consent: meeting?.Consent || null,
      participantNames: meeting?.ParticipantNames || [],
      signRecording: recordings.sign,
    }),
    [meeting?.Consent, meeting?.ParticipantNames, recordings],
  );

  if (state === 'loading') {
    return <Box padding="l">Loading the meeting…</Box>;
  }
  if (state === 'refused') {
    return (
      <Box padding="l">
        <Alert type="error" header="Access denied">
          This meeting is not shared with you. <Link href={`#${SHARED_MEETINGS_PATH}`}>Go to your shared meetings</Link>
          .
        </Alert>
      </Box>
    );
  }
  if (!item) {
    return (
      <Box padding="l">
        <Alert type="error">The meeting could not be read just now. Reload the page to try again.</Alert>
      </Box>
    );
  }

  return (
    <CallPanel
      item={item}
      callTranscriptPerCallId={transcript}
      setToolsOpen={setToolsOpen}
      participantMode
      participantMeeting={participantMeeting}
    />
  );
};

ParticipantMeetingPage.propTypes = {
  client: PropTypes.shape({ graphql: PropTypes.func }),
  callId: PropTypes.string,
  setToolsOpen: PropTypes.func,
};

export default ParticipantMeetingPage;
