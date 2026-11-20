/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
/**
 * The signed-in user's notification settings: the categories they have muted
 * on every meeting, each with a way to show it again. A category is muted from
 * a meeting's Notifications panel.
 */
import React from 'react';
import PropTypes from 'prop-types';
import { Box, Button, Modal, SpaceBetween } from '@cloudscape-design/components';
import { humanize } from './MeetingNotificationsPanel';

const NotificationPreferencesModal = ({ visible, onDismiss, mutedEverywhere, onUnmute }) => (
  <Modal
    visible={visible}
    onDismiss={onDismiss}
    header="Notification settings"
    closeAriaLabel="Close notification settings"
    footer={
      <Box float="right">
        <Button variant="primary" onClick={onDismiss}>
          Close
        </Button>
      </Box>
    }
  >
    <SpaceBetween size="s">
      <Box>
        Meeting notifications appear in a meeting&apos;s Notifications panel, for the people who can see the meeting.
        Muting a category hides it from your view only. Mute a category from a meeting&apos;s Notifications panel.
      </Box>
      <Box>
        The count beside the bell counts the notifications that arrived during this visit to LMA, for meetings you own
        or that are shared with you. It starts again when the page is reloaded.
      </Box>
      {mutedEverywhere.length === 0 ? (
        <Box color="text-body-secondary">No categories are muted on every meeting.</Box>
      ) : (
        <SpaceBetween size="xs">
          <Box variant="h4">Muted on every meeting</Box>
          {mutedEverywhere.map((category) => (
            <SpaceBetween key={category} direction="horizontal" size="xs" alignItems="center">
              <Box>{humanize(category)}</Box>
              <Button variant="link" onClick={() => onUnmute(category)}>
                Show again
              </Button>
            </SpaceBetween>
          ))}
        </SpaceBetween>
      )}
    </SpaceBetween>
  </Modal>
);

NotificationPreferencesModal.propTypes = {
  visible: PropTypes.bool.isRequired,
  onDismiss: PropTypes.func.isRequired,
  mutedEverywhere: PropTypes.arrayOf(PropTypes.string).isRequired,
  onUnmute: PropTypes.func.isRequired,
};

export default NotificationPreferencesModal;
