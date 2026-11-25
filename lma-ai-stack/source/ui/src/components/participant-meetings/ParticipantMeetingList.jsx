/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * A Participant's Meetings list: the meetings shared with them, newest first,
 * read through listParticipantMeetings, which answers only meetings whose
 * sharing list names the caller. Each row opens the read-only meeting page.
 */
import React, { useCallback, useEffect, useRef, useState } from 'react';
import PropTypes from 'prop-types';
import { generateClient } from 'aws-amplify/api';
import { ConsoleLogger } from 'aws-amplify/utils';
import { Alert, Box, Button, Header, Link, StatusIndicator, Table } from '@cloudscape-design/components';

import { listParticipantMeetings } from '../../graphql/queries/participantMeetings';
import { SHARED_MEETINGS_PATH } from '../../routes/constants';
import { formatDuration } from './participant-transcript';

const logger = new ConsoleLogger('ParticipantMeetingList');

const PAGE_SIZE = 50;

/** The page of a shared meeting, as a hash link. */
export const sharedMeetingHref = (callId) => `#${SHARED_MEETINGS_PATH}/${encodeURIComponent(callId)}`;

const STATUS = {
  STARTED: ['in-progress', 'In progress'],
  TRANSCRIBING: ['in-progress', 'In progress'],
  ENDED: ['success', 'Ended'],
  ERRORED: ['error', 'Errored'],
};

export const MeetingStatus = ({ status = undefined }) => {
  const [type, text] = STATUS[status] || ['pending', status || 'Unknown'];
  return <StatusIndicator type={type}>{text}</StatusIndicator>;
};

MeetingStatus.propTypes = { status: PropTypes.string };

const formatStarted = (iso) => (iso ? new Date(iso).toLocaleString() : '');

const ParticipantMeetingList = ({ client: givenClient = undefined }) => {
  const clientRef = useRef(givenClient);
  if (!clientRef.current) clientRef.current = generateClient();
  const [meetings, setMeetings] = useState([]);
  const [nextToken, setNextToken] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  const load = useCallback(async (token) => {
    setLoading(true);
    try {
      const result = await clientRef.current.graphql({
        query: listParticipantMeetings,
        variables: { limit: PAGE_SIZE, nextToken: token || null },
      });
      const page = result?.data?.listParticipantMeetings || {};
      setMeetings((current) => (token ? [...current, ...(page.Meetings || [])] : page.Meetings || []));
      setNextToken(page.nextToken || null);
      setError('');
    } catch (e) {
      logger.error('listParticipantMeetings failed', e);
      setError('Your meetings could not be read just now. Try again in a moment.');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load(null);
  }, [load]);

  return (
    <Table
      header={
        <Header
          variant="h1"
          description="Meetings their owners have shared with you. You can read them; you cannot change them."
          actions={<Button iconName="refresh" ariaLabel="Refresh" onClick={() => load(null)} disabled={loading} />}
        >
          Shared meetings
        </Header>
      }
      loading={loading && meetings.length === 0}
      loadingText="Loading your meetings"
      items={meetings}
      trackBy="CallId"
      empty={
        error ? (
          <Alert type="error">{error}</Alert>
        ) : (
          <Box textAlign="center" color="inherit">
            No meeting has been shared with you yet.
          </Box>
        )
      }
      columnDefinitions={[
        {
          id: 'meeting',
          header: 'Meeting',
          // A render callback the table calls for each row, not a component.
          // eslint-disable-next-line react/no-unstable-nested-components
          cell: (meeting) => <Link href={sharedMeetingHref(meeting.CallId)}>{meeting.CallId}</Link>,
        },
        { id: 'owner', header: 'Shared by', cell: (meeting) => meeting.Owner || '' },
        { id: 'started', header: 'Started', cell: (meeting) => formatStarted(meeting.CreatedAt) },
        // A render callback the table calls for each row, not a component.
        // eslint-disable-next-line react/no-unstable-nested-components
        { id: 'status', header: 'Status', cell: (meeting) => <MeetingStatus status={meeting.Status} /> },
        {
          id: 'duration',
          header: 'Length',
          cell: (meeting) => formatDuration(meeting.TotalConversationDurationMillis),
        },
      ]}
      footer={
        nextToken ? (
          <Box textAlign="center">
            <Button onClick={() => load(nextToken)} loading={loading}>
              Show more
            </Button>
          </Box>
        ) : undefined
      }
    />
  );
};

ParticipantMeetingList.propTypes = { client: PropTypes.shape({ graphql: PropTypes.func }) };

export default ParticipantMeetingList;
