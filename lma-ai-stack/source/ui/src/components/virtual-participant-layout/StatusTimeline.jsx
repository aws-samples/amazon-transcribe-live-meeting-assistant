/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React from 'react';
import PropTypes from 'prop-types';
import { Container, Header, SpaceBetween, Box, Icon, Alert, Badge } from '@awsui/components-react';
import './StatusTimeline.css';

// Status configuration matching VirtualParticipantDetails
const STATUS_CONFIG = {
  INITIALIZING: {
    message: 'Setting up virtual participant...',
    description: 'Preparing connection parameters and authentication',
    icon: 'loading',
    type: 'in-progress',
    color: 'blue',
  },
  CONNECTING: {
    message: 'Connecting to meeting platform...',
    description: 'Establishing connection with meeting platform',
    icon: 'loading',
    type: 'in-progress',
    color: 'blue',
  },
  JOINING: {
    message: 'Joining meeting...',
    description: 'Attempting to enter the meeting room',
    icon: 'loading',
    type: 'in-progress',
    color: 'blue',
  },
  JOINED: {
    message: 'Successfully joined meeting',
    description: 'Virtual participant is now in the meeting',
    icon: 'status-positive',
    type: 'success',
    color: 'green',
  },
  ACTIVE: {
    message: 'Recording in progress',
    description: 'Actively recording meeting audio and generating transcript',
    icon: 'microphone',
    type: 'success',
    color: 'green',
  },
  COMPLETED: {
    message: 'Meeting completed successfully',
    description: 'Meeting ended normally, transcript processing complete',
    icon: 'status-positive',
    type: 'success',
    color: 'green',
  },
  FAILED: {
    message: 'Failed to join meeting',
    description: 'Check error details and troubleshooting steps below',
    icon: 'status-negative',
    type: 'error',
    color: 'red',
  },
  ENDED: {
    message: 'Virtual participant ended by user',
    description: 'Manually terminated by user action',
    icon: 'status-stopped',
    type: 'stopped',
    color: 'grey',
  },
};

const formatTimestamp = (timestamp) => {
  if (!timestamp) return 'N/A';
  const date = new Date(timestamp);
  return date.toLocaleString();
};

const TimelineEntry = ({ entry, isLast }) => {
  const config = STATUS_CONFIG[entry.status] || STATUS_CONFIG.FAILED;

  return (
    <div className={`timeline-entry ${isLast ? 'timeline-entry-last' : ''}`}>
      <div className="timeline-connector">
        <div className={`timeline-icon timeline-icon-${config.type}`}>
          <Icon name={config.icon} variant={config.type === 'error' ? 'error' : 'normal'} size="medium" />
        </div>
        {!isLast && <div className="timeline-line" />}
      </div>

      <div className="timeline-content">
        <div className="timeline-header">
          <SpaceBetween direction="horizontal" size="s" alignItems="center">
            <Badge color={config.color}>{entry.status}</Badge>
            <Box fontSize="body-s" color="text-body-secondary">
              {formatTimestamp(entry.timestamp)}
            </Box>
          </SpaceBetween>
        </div>

        <div className="timeline-message">
          <Box fontWeight="bold">{entry.message || config.message}</Box>
        </div>

        <div className="timeline-description">
          <Box color="text-body-secondary" fontSize="body-s">
            {entry.description || config.description}
          </Box>
        </div>

        {entry.errorDetails && (
          <div className="timeline-error">
            <Alert type="error" size="small">
              <Box fontSize="body-s">
                <strong>Error Details:</strong> {entry.errorDetails}
              </Box>
            </Alert>
          </div>
        )}
      </div>
    </div>
  );
};

TimelineEntry.propTypes = {
  entry: PropTypes.shape({
    status: PropTypes.string.isRequired,
    timestamp: PropTypes.string,
    message: PropTypes.string,
    description: PropTypes.string,
    errorDetails: PropTypes.string,
  }).isRequired,
  isLast: PropTypes.bool.isRequired,
};

const StatusTimeline = ({ history, currentStatus, currentTimestamp }) => {
  // Create timeline entries from history and current status
  const timelineEntries = React.useMemo(() => {
    const entries = [...(history || [])];

    // Add current status if not already in history
    if (currentStatus && !entries.find((entry) => entry.status === currentStatus)) {
      entries.push({
        status: currentStatus,
        timestamp: currentTimestamp,
        message: STATUS_CONFIG[currentStatus]?.message,
        description: STATUS_CONFIG[currentStatus]?.description,
      });
    }

    // Sort by timestamp (oldest first)
    return entries.sort((a, b) => new Date(a.timestamp) - new Date(b.timestamp));
  }, [history, currentStatus, currentTimestamp]);

  if (!timelineEntries || timelineEntries.length === 0) {
    return (
      <Container header={<Header variant="h3">Status Timeline</Header>}>
        <Box textAlign="center" color="text-body-secondary" padding="l">
          No status history available
        </Box>
      </Container>
    );
  }

  return (
    <Container header={<Header variant="h3">Status Timeline</Header>}>
      <div className="status-timeline">
        {timelineEntries.map((entry) => (
          <TimelineEntry
            key={`${entry.status}-${entry.timestamp}`}
            entry={entry}
            isLast={timelineEntries.indexOf(entry) === timelineEntries.length - 1}
          />
        ))}
      </div>
    </Container>
  );
};

StatusTimeline.propTypes = {
  history: PropTypes.arrayOf(
    PropTypes.shape({
      status: PropTypes.string.isRequired,
      timestamp: PropTypes.string,
      message: PropTypes.string,
      description: PropTypes.string,
      errorDetails: PropTypes.string,
    }),
  ),
  currentStatus: PropTypes.string,
  currentTimestamp: PropTypes.string,
};

StatusTimeline.defaultProps = {
  history: [],
  currentStatus: null,
  currentTimestamp: null,
};

export default StatusTimeline;
