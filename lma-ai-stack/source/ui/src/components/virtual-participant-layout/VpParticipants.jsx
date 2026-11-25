/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import React, { useEffect, useState } from 'react';
import PropTypes from 'prop-types';
import { generateClient } from 'aws-amplify/api';
import { ConsoleLogger } from 'aws-amplify/utils';

import MeetingParticipants from '../call-panel/MeetingParticipants';
import {
  getCallParticipants,
  updateVirtualParticipantMeetingParticipants,
} from '../../graphql/queries/meetingParticipants';

const client = generateClient();
const logger = new ConsoleLogger('VpParticipants');

const errorText = (error, fallback) => error?.errors?.[0]?.message || error?.message || fallback;

/**
 * The Participants section of a Virtual Participant's page, at any status.
 *
 * One list at a time. Until the Virtual Participant has created its meeting,
 * the list shown and changed is the one on the Virtual Participant (it reads
 * it again just before it creates the meeting, so a change made while it joins
 * reaches the meeting). Once the meeting record exists, the list shown and
 * changed is the meeting's, the same one its meeting page shows; the server
 * decides which (updateCallParticipants named by VirtualParticipantId). See
 * docs/virtual-participant.md#participant-list.
 */
const VpParticipants = ({ vp, username = '', isAdmin = false, onVpListSaved = () => {} }) => {
  // The meeting's list once the meeting exists; null before (or unreadable).
  const [meetingList, setMeetingList] = useState(null);
  const callId = vp?.CallId || '';
  const owner = vp?.Owner || '';

  useEffect(() => {
    let cancelled = false;
    if (!callId) {
      setMeetingList(null);
      return undefined;
    }
    (async () => {
      try {
        const response = await client.graphql({ query: getCallParticipants, variables: { CallId: callId } });
        const meeting = response?.data?.getCall;
        // Followed only when the meeting has the Virtual Participant's owner, as the server follows it.
        if (!cancelled) setMeetingList(meeting && meeting.Owner === owner ? meeting.Participants || [] : null);
      } catch (error) {
        // No meeting yet, or one this viewer cannot read: the Virtual Participant's list is shown.
        logger.debug('the meeting’s participant list was not read', error);
        if (!cancelled) setMeetingList(null);
      }
    })();
    return () => {
      cancelled = true;
    };
    // The meeting record appears when the Virtual Participant is admitted, so
    // it is looked for again as the status moves on.
  }, [callId, owner, vp?.status]);

  const isOwner = Boolean(username) && owner === username;
  const canManage = isAdmin || isOwner;

  const save = async (participants) => {
    try {
      const response = await client.graphql({
        query: updateVirtualParticipantMeetingParticipants,
        variables: { input: { VirtualParticipantId: vp.id, Participants: participants } },
      });
      const meeting = response?.data?.updateCallParticipants;
      if (meeting) setMeetingList(meeting.Participants || []);
      else onVpListSaved(participants);
      return { ok: true };
    } catch (error) {
      logger.error('could not save the participant list', error);
      return { ok: false, message: errorText(error, 'The change could not be saved.') };
    }
  };

  return (
    <MeetingParticipants
      participants={meetingList ?? vp.participants ?? []}
      callId=""
      virtualParticipantId={vp.id}
      canManage={canManage}
      isOwner={isOwner}
      meetingName={vp.meetingName || ''}
      owner={owner}
      scheduledFor={vp.scheduledFor || ''}
      requests={vp.participantListRequests || []}
      onSave={canManage ? save : undefined}
      description={
        meetingList
          ? "This is the meeting's list, the same one its meeting page shows."
          : 'The Virtual Participant puts this list on the meeting when it creates it.'
      }
    />
  );
};

VpParticipants.propTypes = {
  vp: PropTypes.shape({
    id: PropTypes.string.isRequired,
    CallId: PropTypes.string,
    Owner: PropTypes.string,
    status: PropTypes.string,
    meetingName: PropTypes.string,
    scheduledFor: PropTypes.string,
    participants: PropTypes.arrayOf(PropTypes.shape({ name: PropTypes.string, email: PropTypes.string })),
    participantListRequests: PropTypes.arrayOf(PropTypes.shape({ name: PropTypes.string, email: PropTypes.string })),
  }).isRequired,
  username: PropTypes.string,
  isAdmin: PropTypes.bool,
  // The Virtual Participant's own list was changed (no meeting yet).
  onVpListSaved: PropTypes.func,
};

export default VpParticipants;
