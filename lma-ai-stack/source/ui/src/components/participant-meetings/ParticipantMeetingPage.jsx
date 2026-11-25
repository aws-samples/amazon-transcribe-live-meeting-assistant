/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The read-only page of a meeting shared with a Participant.
 *
 * Everything on it comes from the Participant view (getParticipantMeeting and
 * the operations beside it), which the server builds from an allowlist: the
 * transcript, live while the meeting is in progress (onAddParticipantTranscriptSegment),
 * the summary and its action items, the agenda, the meeting chat lines, the
 * recordings (links the server signs for this meeting), the consent record,
 * and the names on the participant list. The owner's own material is not sent
 * to this page at all, and the page offers no control that changes the meeting.
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
import {
  Alert,
  Box,
  Button,
  ColumnLayout,
  Container,
  ExpandableSection,
  Header,
  Link,
  SpaceBetween,
  Table,
} from '@cloudscape-design/components';

import SummaryMarkdown from '../summary-markdown/SummaryMarkdown';
import useStrandsChatBridge from '../../hooks/use-strands-chat-bridge';
import {
  getParticipantMeeting,
  getParticipantMeetingRecordings,
  getParticipantMeetingTranscript,
  onAddParticipantTranscriptSegment,
} from '../../graphql/queries/participantMeetings';
import { SHARED_MEETINGS_PATH } from '../../routes/constants';
import { MeetingStatus } from './ParticipantMeetingList';
import { displayLines, formatDuration, mergeSegments, mergeSegment } from './participant-transcript';

const logger = new ConsoleLogger('ParticipantMeetingPage');

const REFRESH_MS = 30000;
// The most transcript pages read in one go; a meeting is a few pages at most.
const MAX_TRANSCRIPT_PAGES = 50;

const isLive = (meeting) => meeting && meeting.Status !== 'ENDED' && meeting.Status !== 'ERRORED';

const refusal = (error) => {
  const text = `${error?.message || ''} ${(error?.errors || []).map((e) => e.message).join(' ')}`;
  return /Unauthorized|not authorized|Not Authorized/i.test(text);
};

const Transcript = ({ lines, deletedAt = undefined }) => {
  if (lines.length === 0) {
    return (
      <Box color="text-status-inactive">
        {deletedAt ? 'The transcript was deleted after the summary was written.' : 'Nothing has been transcribed yet.'}
      </Box>
    );
  }
  return (
    <div className="participant-transcript" aria-label="Transcript">
      {lines.map((line) => (
        <Box key={line.key} padding={{ vertical: 'xxs' }} color={line.partial ? 'text-status-inactive' : undefined}>
          <Box variant="small" display="inline">
            {line.time}{' '}
          </Box>
          <Box variant="strong" display="inline">
            {line.chat ? `[chat] ${line.speaker}` : line.speaker}:
          </Box>{' '}
          {line.text}
        </Box>
      ))}
    </div>
  );
};

Transcript.propTypes = {
  lines: PropTypes.arrayOf(PropTypes.shape({ key: PropTypes.string })).isRequired,
  deletedAt: PropTypes.string,
};

const Recordings = ({ meeting, client }) => {
  const [links, setLinks] = useState(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  // A player that fails is given fresh links once (the hour-long ones may have
  // lapsed); a second failure shows the error rather than asking again.
  const [retried, setRetried] = useState(false);
  const has = meeting.HasAudioRecording || meeting.HasVideoRecording;

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const result = await client.graphql({
        query: getParticipantMeetingRecordings,
        variables: { CallId: meeting.CallId },
      });
      setLinks(result?.data?.getParticipantMeetingRecordings || []);
      setError('');
    } catch (e) {
      logger.error('getParticipantMeetingRecordings failed', e);
      setError('The recordings could not be opened just now.');
    } finally {
      setLoading(false);
    }
  }, [client, meeting.CallId]);

  const onPlayerError = useCallback(() => {
    if (retried) {
      setLinks(null);
      setError('The recording could not be played. Try again later.');
      return;
    }
    setRetried(true);
    load();
  }, [retried, load]);

  if (!has) {
    return (
      <Box color="text-status-inactive">
        {meeting.RecordingsDeletedAt
          ? 'The recordings were deleted after the summary was written.'
          : 'This meeting has no recording.'}
      </Box>
    );
  }
  if (!links) {
    return (
      <SpaceBetween size="s">
        {error && <Alert type="error">{error}</Alert>}
        <Button onClick={load} loading={loading}>
          Play recordings
        </Button>
      </SpaceBetween>
    );
  }
  return (
    <SpaceBetween size="m">
      {links.map((link) => (
        <div key={`${link.Kind}-${link.SessionNumber ?? 'x'}-${link.Url.split('?')[0]}`}>
          <Box variant="awsui-key-label">
            {link.Kind === 'VIDEO' ? 'Video' : 'Audio'}
            {link.SessionNumber ? `, session ${link.SessionNumber}` : ''}
          </Box>
          {link.Kind === 'VIDEO' ? (
            // eslint-disable-next-line jsx-a11y/media-has-caption
            <video controls preload="metadata" src={link.Url} style={{ maxWidth: '100%' }} onError={onPlayerError} />
          ) : (
            // eslint-disable-next-line jsx-a11y/media-has-caption
            <audio controls preload="metadata" src={link.Url} onError={onPlayerError} />
          )}
        </div>
      ))}
      <Box variant="small" color="text-status-inactive">
        Each link works for an hour; reopen the page to play again after that.
      </Box>
    </SpaceBetween>
  );
};

Recordings.propTypes = {
  meeting: PropTypes.shape({
    CallId: PropTypes.string,
    HasAudioRecording: PropTypes.bool,
    HasVideoRecording: PropTypes.bool,
    RecordingsDeletedAt: PropTypes.string,
  }).isRequired,
  client: PropTypes.shape({ graphql: PropTypes.func }).isRequired,
};

const Consent = ({ consent = undefined }) => {
  if (!consent) {
    return <Box color="text-status-inactive">No consent was asked for in this meeting.</Box>;
  }
  return (
    <SpaceBetween size="s">
      <ColumnLayout columns={3} variant="text-grid">
        <div>
          <Box variant="awsui-key-label">Mode</Box>
          <div>{consent.Mode || ''}</div>
        </div>
        <div>
          <Box variant="awsui-key-label">Status</Box>
          <div>{consent.Status || ''}</div>
        </div>
        <div>
          <Box variant="awsui-key-label">Recording and transcription allowed</Box>
          <div>{consent.CaptureAllowed ? 'Yes' : 'No'}</div>
        </div>
      </ColumnLayout>
      {consent.NoticeText && (
        <div>
          <Box variant="awsui-key-label">Notice</Box>
          <Box>{consent.NoticeText}</Box>
        </div>
      )}
      <Table
        variant="embedded"
        items={consent.Replies || []}
        empty={<Box color="text-status-inactive">No replies were recorded.</Box>}
        columnDefinitions={[
          { id: 'name', header: 'Participant', cell: (reply) => reply.ParticipantName || '' },
          { id: 'decision', header: 'Reply', cell: (reply) => reply.Decision || '' },
          { id: 'source', header: 'Given by', cell: (reply) => reply.Source || '' },
          {
            id: 'when',
            header: 'When',
            cell: (reply) => (reply.OccurredAt ? new Date(reply.OccurredAt).toLocaleString() : ''),
          },
        ]}
      />
    </SpaceBetween>
  );
};

Consent.propTypes = {
  consent: PropTypes.shape({
    Mode: PropTypes.string,
    Status: PropTypes.string,
    CaptureAllowed: PropTypes.bool,
    NoticeText: PropTypes.string,
    Replies: PropTypes.arrayOf(PropTypes.shape({})),
  }),
};

/** The meeting assistant panel: the same chat frame the meeting page uses, for this meeting. */
const Assistant = ({ callId }) => {
  useStrandsChatBridge(callId);
  return (
    <iframe
      title="Meeting assistant"
      src={`/strands-chat.html?callId=${encodeURIComponent(callId)}`}
      sandbox="allow-scripts allow-same-origin"
      style={{ width: '100%', height: '520px', border: 0 }}
    />
  );
};

Assistant.propTypes = { callId: PropTypes.string.isRequired };

const ParticipantMeetingPage = ({ client: givenClient = undefined, callId: givenCallId = undefined }) => {
  const params = useParams();
  const callId = givenCallId || params.callId;
  const clientRef = useRef(givenClient);
  if (!clientRef.current) clientRef.current = generateClient();
  const client = clientRef.current;

  const [meeting, setMeeting] = useState(null);
  const [lines, setLines] = useState([]);
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
    setLines((current) => mergeSegments(collected, current));
  }, [client, callId]);

  useEffect(() => {
    setLines([]);
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
          if (segment) setLines((current) => mergeSegment(current, segment));
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

  const shown = useMemo(
    () => displayLines(lines, meeting?.SpeakerIdentities, meeting?.OtherParticipantsName),
    [lines, meeting?.SpeakerIdentities, meeting?.OtherParticipantsName],
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
  if (state === 'error' && !meeting) {
    return (
      <Box padding="l">
        <Alert type="error">The meeting could not be read just now. Reload the page to try again.</Alert>
      </Box>
    );
  }

  return (
    <SpaceBetween size="l">
      <Container
        header={
          <Header variant="h1" description={`Shared by ${meeting.Owner || 'its owner'}. Read-only.`}>
            {meeting.CallId}
          </Header>
        }
      >
        <ColumnLayout columns={4} variant="text-grid">
          <div>
            <Box variant="awsui-key-label">Status</Box>
            <MeetingStatus status={meeting.Status} />
          </div>
          <div>
            <Box variant="awsui-key-label">Started</Box>
            <div>{meeting.CreatedAt ? new Date(meeting.CreatedAt).toLocaleString() : ''}</div>
          </div>
          <div>
            <Box variant="awsui-key-label">Length</Box>
            <div>{formatDuration(meeting.TotalConversationDurationMillis)}</div>
          </div>
          <div>
            <Box variant="awsui-key-label">Platform</Box>
            <div>{meeting.MeetingPlatform || ''}</div>
          </div>
        </ColumnLayout>
        {(meeting.ParticipantNames || []).length > 0 && (
          <Box margin={{ top: 's' }}>
            <Box variant="awsui-key-label">Participants</Box>
            <div>{meeting.ParticipantNames.join(', ')}</div>
          </Box>
        )}
      </Container>

      <Container header={<Header variant="h2">Summary</Header>}>
        {meeting.CallSummaryText ? (
          <SpaceBetween size="m">
            <SummaryMarkdown callId={meeting.CallId} keyframes="text">
              {meeting.CallSummaryText}
            </SummaryMarkdown>
            {(meeting.ActionItems || []).length > 0 && (
              <Table
                variant="embedded"
                header={<Header variant="h3">Action items</Header>}
                items={meeting.ActionItems}
                columnDefinitions={[
                  { id: 'text', header: 'Action', cell: (item) => item.Text },
                  { id: 'owner', header: 'Owner', cell: (item) => item.Owner || '' },
                  { id: 'due', header: 'Due', cell: (item) => item.DueText || item.DueDate || '' },
                ]}
              />
            )}
          </SpaceBetween>
        ) : (
          <Box color="text-status-inactive">
            {meeting.SummaryStatus === 'running'
              ? 'The summary is being written.'
              : 'There is no summary yet; it is written when the meeting ends.'}
          </Box>
        )}
      </Container>

      {meeting.Agenda && (
        <Container header={<Header variant="h2">Agenda</Header>}>
          <Box>
            <pre style={{ whiteSpace: 'pre-wrap', fontFamily: 'inherit', margin: 0 }}>{meeting.Agenda}</pre>
          </Box>
        </Container>
      )}

      <Container
        header={
          <Header variant="h2" description={live ? 'Live: new lines appear as they are transcribed.' : undefined}>
            Transcript
          </Header>
        }
      >
        <Transcript lines={shown} deletedAt={meeting.TranscriptDeletedAt} />
      </Container>

      <Container header={<Header variant="h2">Recordings</Header>}>
        <Recordings meeting={meeting} client={client} />
      </Container>

      <Container header={<Header variant="h2">Consent record</Header>}>
        <Consent consent={meeting.Consent} />
      </Container>

      <ExpandableSection
        variant="container"
        headerText="Meeting assistant"
        headerDescription="Ask about this meeting. Your questions and its answers are seen by you only."
      >
        <Assistant callId={meeting.CallId} />
      </ExpandableSection>
    </SpaceBetween>
  );
};

ParticipantMeetingPage.propTypes = {
  client: PropTypes.shape({ graphql: PropTypes.func }),
  callId: PropTypes.string,
};

export default ParticipantMeetingPage;
