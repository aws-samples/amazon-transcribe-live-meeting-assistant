/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Shows, above the meeting's tabs, the guidance an MCP client pushed to the
 * meeting while the page is open (the push_meeting_guidance tool), each as a
 * dismissible notification. Only the meeting's owner, the people it is shared
 * with and Admins receive it. It is never posted to the meeting's chat.
 */
import React from 'react';
import PropTypes from 'prop-types';
import { Box, Flashbar } from '@cloudscape-design/components';

import useMeetingGuidance from '../../hooks/use-meeting-guidance';

/** The Flashbar type each guidance level is shown as. */
export const LEVEL_TYPES = { INFO: 'info', WARNING: 'warning', URGENT: 'error' };

const MeetingGuidanceBanner = ({ callId, client = undefined }) => {
  const { items, dismiss } = useMeetingGuidance(callId, { client });
  if (items.length === 0) return null;

  const flashItems = items.map((item) => ({
    id: item.GuidanceId,
    type: LEVEL_TYPES[item.Level] || 'info',
    header: item.Title || 'Guidance for this meeting',
    content: (
      <>
        <Box variant="p">{item.Text}</Box>
        <Box variant="small" color="text-body-secondary">
          {item.CreatedBy ? `From ${item.CreatedBy}, through an MCP client` : 'From an MCP client'}
          {item.CreatedAt ? ` at ${new Date(item.CreatedAt).toLocaleTimeString()}` : ''}. Only people who can see this
          meeting in LMA see it.
        </Box>
      </>
    ),
    dismissible: true,
    dismissLabel: 'Dismiss guidance',
    onDismiss: () => dismiss(item.GuidanceId),
  }));

  return (
    <div data-testid="meeting-guidance">
      <Flashbar items={flashItems} />
    </div>
  );
};

MeetingGuidanceBanner.propTypes = {
  callId: PropTypes.string.isRequired,
  // For tests: the GraphQL client to subscribe with.
  // eslint-disable-next-line react/forbid-prop-types
  client: PropTypes.object,
};

export default MeetingGuidanceBanner;
