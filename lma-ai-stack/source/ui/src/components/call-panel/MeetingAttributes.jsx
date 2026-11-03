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
 * The recordings are played by the one player on the Transcript tab, kept in
 * step with the transcript; here each has a Play that opens that tab, and a
 * link to the file itself. No second player, so only one thing ever plays.
 */
import React, { useState } from 'react';
import PropTypes from 'prop-types';
import { ConsoleLogger } from 'aws-amplify/utils';
import {
  Box,
  Button,
  Container,
  CopyToClipboard,
  Header,
  KeyValuePairs,
  SpaceBetween,
  StatusIndicator,
} from '@cloudscape-design/components';

import useAppContext from '../../contexts/app';
import generateS3PresignedUrl from '../common/generate-s3-presigned-url';
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

const logger = new ConsoleLogger('MeetingAttributes');

/**
 * A recording's actions: Play (on the Transcript tab) and Open, which opens
 * the file in a new browser tab, to watch or save. The tab is opened within
 * the click, before the link is signed, because a browser blocks one opened
 * after an await; the signed link lasts minutes, so it is signed on demand.
 */
const RecordingActions = ({ url, kind, onPlay = undefined, sign = generateS3PresignedUrl }) => {
  const { currentCredentials } = useAppContext();
  const [error, setError] = useState(false);
  const open = async () => {
    setError(false);
    const tab = window.open('', '_blank');
    if (tab) tab.opener = null;
    try {
      const signed = await sign(url, currentCredentials);
      if (tab) tab.location.href = signed;
    } catch (e) {
      logger.error('could not sign the recording link', e);
      if (tab) tab.close();
      setError(true);
    }
  };
  return (
    <SpaceBetween direction="horizontal" size="xs" alignItems="center">
      {onPlay && (
        <Button variant="inline-link" iconName="play" onClick={onPlay} ariaLabel={`Play the ${kind} recording`}>
          Play
        </Button>
      )}
      <Button
        variant="inline-link"
        iconName="external"
        iconAlign="right"
        onClick={open}
        ariaLabel={`Open the ${kind} recording file in a new tab`}
      >
        Open file
      </Button>
      {error && <StatusIndicator type="error">Could not open the recording</StatusIndicator>}
    </SpaceBetween>
  );
};

RecordingActions.propTypes = {
  url: PropTypes.string.isRequired,
  kind: PropTypes.string.isRequired,
  onPlay: PropTypes.func,
  sign: PropTypes.func,
};

const MeetingAttributes = ({ item, durationMillis, platform = '', onPlay = undefined }) => {
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

  if (item.videoRecordingUrl && recordingsReady) {
    items.push({
      label: 'Video recording',
      value: <RecordingActions url={item.videoRecordingUrl} kind="video" onPlay={onPlay} />,
    });
  }
  if (item.recordingUrl && recordingsReady) {
    items.push({
      label: 'Audio recording',
      value: (
        // The page's player plays the video when there is one; the audio is
        // played from the player's Audio only switch.
        <RecordingActions url={item.recordingUrl} kind="audio" onPlay={item.videoRecordingUrl ? undefined : onPlay} />
      ),
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
  onPlay: PropTypes.func,
};

export default MeetingAttributes;
