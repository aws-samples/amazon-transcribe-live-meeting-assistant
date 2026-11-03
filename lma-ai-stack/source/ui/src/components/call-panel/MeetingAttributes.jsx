/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The meeting's technical attributes, on the Details tab: the full Meeting ID,
 * the stored times as UTC (with the local time beside them), the length, who
 * owns it and who it is shared with, and the recordings.
 *
 * The audio and video players are here until the meeting page has a player
 * kept in step with the transcript. A key frame's "Play recording from here"
 * opens this tab and plays the video from that time; selecting a frame or a
 * chapter only moves the video there (VideoPlayer's `play: false`).
 */
import React from 'react';
import PropTypes from 'prop-types';
import { Box, Container, CopyToClipboard, Header, KeyValuePairs, StatusIndicator } from '@cloudscape-design/components';

import RecordingPlayer from '../recording-player';
import VideoPlayer from '../video-player';
import { IN_PROGRESS_STATUS } from '../common/get-recording-status';
import { formatDuration, formatMeetingStart } from '../common/meeting-header';
import { sharedWithEntries } from '../common/shared-with';

const timeValue = (iso) => {
  if (!iso) return '—';
  const local = formatMeetingStart(iso);
  return (
    <>
      <div>{iso}</div>
      {local && (
        <Box variant="small" color="text-body-secondary">
          {local}
        </Box>
      )}
    </>
  );
};

const MeetingAttributes = ({ item, durationMillis, platform = '', videoSeekRequest = null }) => {
  const recordingsReady = item.recordingStatusLabel !== IN_PROGRESS_STATUS;
  const storedMillis = Number(item.totalConversationDurationMillis) || 0;
  const sharedWith = sharedWithEntries(item.sharedWith);

  const items = [
    {
      label: 'Meeting ID',
      value: <CopyToClipboard variant="inline" textToCopy={item.callId} copySuccessText="Meeting ID copied" />,
    },
    {
      label: 'Status',
      value: <StatusIndicator type={item.recordingStatusIcon}>{item.recordingStatusLabel}</StatusIndicator>,
    },
    {
      label: 'Duration',
      value: (
        <>
          <div>{formatDuration(durationMillis)}</div>
          {Math.floor(storedMillis / 1000) !== Math.floor(durationMillis / 1000) && (
            // Said, rather than silently corrected: the meeting list and the
            // MCP tools still read the stored figure.
            <Box variant="small" color="text-body-secondary">
              {`From the transcript; the stored duration is ${formatDuration(storedMillis)}`}
            </Box>
          )}
        </>
      ),
    },
    { label: 'Started (UTC)', value: timeValue(item.initiationTimeStamp) },
    { label: 'Last updated (UTC)', value: timeValue(item.updatedAt) },
    ...(platform ? [{ label: 'Platform', value: platform }] : []),
    { label: 'Owner', value: item.owner || '—' },
    { label: 'Shared with', value: sharedWith.length ? sharedWith.join(', ') : '—' },
  ];

  if (item.recordingUrl && recordingsReady) {
    items.push({ label: 'Audio recording', value: <RecordingPlayer recordingUrl={item.recordingUrl} /> });
  }
  if (item.videoRecordingUrl && recordingsReady) {
    items.push({
      label: 'Video recording',
      value: <VideoPlayer videoRecordingUrl={item.videoRecordingUrl} seekRequest={videoSeekRequest} />,
    });
  }

  return (
    <Container header={<Header variant="h3">Meeting attributes</Header>}>
      <KeyValuePairs columns={3} items={items} />
    </Container>
  );
};

MeetingAttributes.propTypes = {
  // eslint-disable-next-line react/forbid-prop-types
  item: PropTypes.object.isRequired,
  durationMillis: PropTypes.number.isRequired,
  platform: PropTypes.string,
  videoSeekRequest: PropTypes.shape({
    seconds: PropTypes.number,
    at: PropTypes.number,
    reveal: PropTypes.bool,
    play: PropTypes.bool,
  }),
};

export default MeetingAttributes;
