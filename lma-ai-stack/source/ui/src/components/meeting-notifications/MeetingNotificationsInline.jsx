/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
/**
 * A meeting's notifications above the tabs, in a section that can be folded,
 * for a meeting page without the application's Notifications drawer (an
 * embedded one). Shown once the meeting has a notification.
 */
import React from 'react';
import PropTypes from 'prop-types';
import { ExpandableSection } from '@cloudscape-design/components';
import useMeetingNotifications from '../../hooks/use-meeting-notifications';
import MeetingNotificationsPanel from './MeetingNotificationsPanel';

const MeetingNotificationsInline = ({ callId, client = undefined }) => {
  const notifications = useMeetingNotifications(callId, { client });
  if (notifications.items.length === 0) return null;
  return (
    <ExpandableSection
      variant="container"
      defaultExpanded
      headerText={`Notifications (${notifications.visible.length})`}
      data-testid="meeting-notifications-inline"
    >
      <MeetingNotificationsPanel notifications={notifications} />
    </ExpandableSection>
  );
};

MeetingNotificationsInline.propTypes = {
  callId: PropTypes.string.isRequired,
  // For tests: the GraphQL client to use.
  // eslint-disable-next-line react/forbid-prop-types
  client: PropTypes.object,
};

export default MeetingNotificationsInline;
