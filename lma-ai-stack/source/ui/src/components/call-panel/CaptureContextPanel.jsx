/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * How the meeting was captured and processed, on the Details tab: where its
 * audio came from (and the Virtual Participant, when one joined), whether its
 * voices were told apart, which recordings were kept, the consent setting that
 * applied, and what the deployment's Retention settings do to its transcript --
 * so that a transcript that is missing, or that stops, can be explained from
 * the page. Read from the meeting record and what the page already loaded
 * (common/meeting-context.js).
 */
import React from 'react';
import PropTypes from 'prop-types';
import { Box, Button, Container, Header, KeyValuePairs, Link, StatusIndicator } from '@cloudscape-design/components';

import { IN_PROGRESS_STATUS } from '../common/get-recording-status';
import { MODE_LABELS, statusIndicatorType } from '../common/meeting-consent';
import {
  SOURCE_VIRTUAL_PARTICIPANT,
  captureSource,
  consentSummary,
  speakerSeparation,
  transcriptRetention,
} from '../common/meeting-context';

export const CONSENT_SETTING_ID = 'meeting-consent-setting';

const formatTime = (iso) => {
  const when = new Date(iso);
  return Number.isNaN(when.getTime()) ? iso : when.toLocaleString();
};

const Note = ({ children }) => (
  <Box variant="small" color="text-body-secondary">
    {children}
  </Box>
);

Note.propTypes = { children: PropTypes.node.isRequired };

const plural = (count, one, many) => `${count} ${count === 1 ? one : many}`;

const sourceValue = (source, vp, platform) => (
  <>
    <div>{source.kind === SOURCE_VIRTUAL_PARTICIPANT && platform ? `${source.label} · ${platform}` : source.label}</div>
    {source.inferred && <Note>Read from the form of the Meeting ID; the meeting record does not name its source.</Note>}
    {vp?.id && (
      <Link href={`#/virtual-participant/${vp.id}`} fontSize="body-s">
        {`Virtual Participant ${vp.id}`}
      </Link>
    )}
  </>
);

const separationValue = (separation, transcriptDeleted) => {
  if (!separation) {
    return transcriptDeleted ? 'Not known: the transcript was deleted' : 'Not known yet: no speech in the transcript';
  }
  if (!separation.separated) {
    return (
      <>
        <div>No separated voices</div>
        <Note>Each channel is one speaker in the transcript.</Note>
      </>
    );
  }
  return (
    <>
      <div>{`On: ${plural(separation.voices, 'voice', 'voices')} told apart`}</div>
      {separation.splitChannel && <Note>More than one voice on a channel; name them on the Speakers panel.</Note>}
    </>
  );
};

const recordingValue = (url, { live, off, kind }) => {
  if (url) return <StatusIndicator type="success">Recorded</StatusIndicator>;
  if (off) return <StatusIndicator type="stopped">Off for this Virtual Participant</StatusIndicator>;
  if (live)
    return <StatusIndicator type="pending">{`Stored when the meeting ends, if ${kind} is recorded`}</StatusIndicator>;
  return <StatusIndicator type="stopped">Not recorded</StatusIndicator>;
};

const consentValue = (summary, consent, onShowConsentRecord) => {
  if (!summary.known) return <StatusIndicator type="loading">Reading</StatusIndicator>;
  if (!summary.enforced) {
    return (
      <>
        <div>Off</div>
        <Note>No consent notice was required, and recording and transcription were not held back for one.</Note>
      </>
    );
  }
  return (
    <>
      <div>
        <strong>{MODE_LABELS[consent.Mode] || consent.Mode}</strong>
      </div>
      <StatusIndicator type={statusIndicatorType(consent.Status)}>{summary.statusLabel}</StatusIndicator>
      {onShowConsentRecord && (
        <div>
          <Button variant="inline-link" onClick={onShowConsentRecord}>
            View the consent record
          </Button>
        </div>
      )}
    </>
  );
};

const retentionValue = (retention) => {
  const record = retention.recordExpiresAt && (
    <Note>{`The meeting record is kept until ${formatTime(retention.recordExpiresAt)}.`}</Note>
  );
  if (retention.state === 'deleted') {
    const counts =
      retention.segments != null || retention.files != null
        ? ` ${[
            retention.segments != null ? plural(retention.segments, 'segment', 'segments') : null,
            retention.files != null ? plural(retention.files, 'file', 'files') : null,
          ]
            .filter(Boolean)
            .join(' and ')} removed.`
        : '';
    return (
      <>
        <StatusIndicator type="stopped">{`Deleted ${formatTime(retention.at)}`}</StatusIndicator>
        <Note>
          {`Deleted on schedule by the Retention setting "Delete the transcript after the summary".${counts} ` +
            'The summary, the recordings and the meeting record are kept.'}
        </Note>
        {record}
      </>
    );
  }
  if (retention.state === 'scheduled') {
    return (
      <>
        <StatusIndicator type="warning">{`To be deleted ${formatTime(retention.at)}`}</StatusIndicator>
        <Note>
          By the Retention setting &quot;Delete the transcript after the summary&quot;. Download it from the Download
          menu before then if you need a copy.
        </Note>
        {record}
      </>
    );
  }
  return (
    <>
      <div>Kept for the deployment&apos;s days to keep transcript segments</div>
      <Note>Each segment expires that long after it was written. Not deleted after the summary.</Note>
      {record}
    </>
  );
};

const CaptureContextPanel = ({
  item,
  vp = null,
  platform = '',
  roster = [],
  consentState = null,
  onShowConsentRecord = undefined,
}) => {
  const live = item.recordingStatusLabel === IN_PROGRESS_STATUS;
  const source = captureSource(item, vp);
  const retention = transcriptRetention(item);
  const consent = consentSummary(consentState);
  const videoOff = Boolean(vp) && vp.enableVideoRecording === false;

  const items = [
    { label: 'Captured with', value: sourceValue(source, vp, platform) },
    { label: 'Speaker separation', value: separationValue(speakerSeparation(roster), retention.state === 'deleted') },
    { label: 'Audio recording', value: recordingValue(item.recordingUrl, { live, kind: 'audio' }) },
    { label: 'Video recording', value: recordingValue(item.videoRecordingUrl, { live, off: videoOff, kind: 'video' }) },
    {
      label: 'Consent',
      value: <div id={CONSENT_SETTING_ID}>{consentValue(consent, consentState?.consent, onShowConsentRecord)}</div>,
    },
    { label: 'Transcript retention', value: retentionValue(retention) },
  ];

  return (
    <Container
      header={
        <Header
          variant="h3"
          description="The transcription engine and language are not recorded on the meeting, so they are not shown."
        >
          Capture and processing
        </Header>
      }
    >
      <KeyValuePairs columns={3} items={items} />
    </Container>
  );
};

CaptureContextPanel.propTypes = {
  // eslint-disable-next-line react/forbid-prop-types
  item: PropTypes.object.isRequired,
  // The Virtual Participant linked to the meeting, when one is.
  // eslint-disable-next-line react/forbid-prop-types
  vp: PropTypes.object,
  platform: PropTypes.string,
  // buildSpeakerRoster's rows.
  // eslint-disable-next-line react/forbid-prop-types
  roster: PropTypes.arrayOf(PropTypes.object),
  // What useMeetingConsent returns.
  // eslint-disable-next-line react/forbid-prop-types
  consentState: PropTypes.object,
  onShowConsentRecord: PropTypes.func,
};

export default CaptureContextPanel;
