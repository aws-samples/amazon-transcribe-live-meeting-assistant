/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The Rejoin action on the Virtual Participant page: relaunch a Virtual
 * Participant that has left its meeting into the same online meeting, adding to
 * the same LMA meeting, which is reopened if it has ended. The server holds the
 * rules (the meeting's owner or an Admin; see rejoinVirtualParticipant); this
 * only offers the action where they can be met. See
 * docs/virtual-participant.md#rejoin.
 */
import React, { useState } from 'react';
import PropTypes from 'prop-types';
import { generateClient } from 'aws-amplify/api';
import { ConsoleLogger } from 'aws-amplify/utils';
import { Alert, Box, Button, Modal, SpaceBetween } from '@cloudscape-design/components';

const logger = new ConsoleLogger('RejoinVirtualParticipant');
const client = generateClient();

export const rejoinVirtualParticipantMutation = `
  mutation RejoinVirtualParticipant($input: RejoinVirtualParticipantInput!) {
    rejoinVirtualParticipant(input: $input) {
      id
      status
      errorMessage
      updatedAt
      owner
      Owner
      SharedWith
      CallId
      vncReady
      sessionNumber
      rejoinedAt
    }
  }
`;

/** The statuses of a Virtual Participant that has left its meeting. */
export const REJOINABLE_STATUSES = ['ENDED', 'COMPLETED', 'FAILED'];

/**
 * Whether to offer Rejoin: the Virtual Participant has left a meeting it
 * started (it has a CallId), and the viewer may manage it.
 */
export const canRejoin = (vp, canManage) =>
  !!canManage && !!vp && !!vp.CallId && REJOINABLE_STATUSES.includes(vp.status);

/** The message an AppSync error carries, or a general one. */
const errorText = (error) =>
  error?.errors?.[0]?.message || error?.message || 'The Virtual Participant could not rejoin the meeting.';

const RejoinVirtualParticipant = ({ vp = null, canManage = false, onRejoined = null }) => {
  const [visible, setVisible] = useState(false);
  const [rejoining, setRejoining] = useState(false);
  const [error, setError] = useState(null);

  if (!canRejoin(vp, canManage)) return null;

  const close = () => {
    if (rejoining) return;
    setVisible(false);
    setError(null);
  };

  const rejoin = async () => {
    setRejoining(true);
    setError(null);
    try {
      const result = await client.graphql({
        query: rejoinVirtualParticipantMutation,
        variables: { input: { id: vp.id } },
      });
      setVisible(false);
      if (onRejoined) onRejoined(result?.data?.rejoinVirtualParticipant);
    } catch (err) {
      logger.error('Rejoin failed', err);
      setError(errorText(err));
    } finally {
      setRejoining(false);
    }
  };

  return (
    <>
      <Button iconName="redo" onClick={() => setVisible(true)}>
        Rejoin
      </Button>
      <Modal
        visible={visible}
        onDismiss={close}
        header="Rejoin the meeting?"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={close} disabled={rejoining}>
                Cancel
              </Button>
              <Button variant="primary" onClick={rejoin} loading={rejoining}>
                Rejoin meeting
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween size="s">
          <Box>
            The Virtual Participant joins <strong>{vp.meetingName || 'the meeting'}</strong> again and adds to the same
            LMA meeting: the transcript continues after what is already there, and the meeting shows as in progress
            again. When it leaves, the summary is generated again for the whole meeting.
          </Box>
          <Box color="text-body-secondary">
            It posts the meeting&apos;s consent notice again when it joins, as on any join.
          </Box>
          {error && <Alert type="error">{error}</Alert>}
        </SpaceBetween>
      </Modal>
    </>
  );
};

RejoinVirtualParticipant.propTypes = {
  vp: PropTypes.shape({
    id: PropTypes.string,
    status: PropTypes.string,
    CallId: PropTypes.string,
    meetingName: PropTypes.string,
  }),
  canManage: PropTypes.bool,
  onRejoined: PropTypes.func,
};

export default RejoinVirtualParticipant;
