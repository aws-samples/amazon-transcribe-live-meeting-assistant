/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * How the meeting was captured and processed, on the Details tab: where its
 * audio came from (and the Virtual Participant, when one joined), whether its
 * voices were told apart, which recordings were kept, the consent setting that
 * applied, and what the deployment's Retention settings do to its transcript --
 * so that a transcript that is missing, or that stops, can be explained from
 * the page. Read from the meeting record and what the page already loaded
 * (common/meeting-context.js). The capture source, transcription engine,
 * language and speaker separation are the ones the meeting recorded when it
 * started; a meeting started before those were recorded shows its source and
 * speaker separation as inferred, and says so, and no engine or language. The
 * detected languages are the ones the speech engine identified in the meeting
 * (common/detected-languages.js), shown only when it identified any.
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
  hasRecordedCaptureContext,
  recordedSpeakerSeparation,
  speakerSeparation,
  transcriptionEngine,
  transcriptionLanguage,
  transcriptRetention,
} from '../common/meeting-context';
import { detectedLanguagesText, meetingLanguages } from '../common/detected-languages';

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
    {source.inferred && <Note>Inferred from the form of the Meeting ID; the meeting record does not name it.</Note>}
    {vp?.id && (
      <Link href={`#/virtual-participant/${vp.id}`} fontSize="body-s">
        {`Virtual Participant ${vp.id}`}
      </Link>
    )}
  </>
);

const observedVoices = (separation) =>
  separation?.separated ? `${plural(separation.voices, 'voice', 'voices')} told apart in the transcript.` : null;

const separationValue = (separation, transcriptDeleted, recordedSetting) => {
  if (recordedSetting) {
    const observed = observedVoices(separation);
    return (
      <>
        <div>{recordedSetting}</div>
        {observed && <Note>{observed}</Note>}
      </>
    );
  }
  if (!separation) {
    return transcriptDeleted ? 'Not known: the transcript was deleted' : 'Not known yet: no speech in the transcript';
  }
  if (!separation.separated) {
    return (
      <>
        <div>No separated voices</div>
        <Note>Inferred from the transcript: each channel is one speaker in it.</Note>
      </>
    );
  }
  return (
    <>
      <div>{`On: ${plural(separation.voices, 'voice', 'voices')} told apart`}</div>
      <Note>Inferred from the voice labels in the transcript.</Note>
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
  languages = null,
  consentState = null,
  onShowConsentRecord = undefined,
}) => {
  const live = item.recordingStatusLabel === IN_PROGRESS_STATUS;
  const source = captureSource(item, vp);
  const retention = transcriptRetention(item);
  const consent = consentSummary(consentState);
  const videoOff = Boolean(vp) && vp.enableVideoRecording === false;
  const engine = transcriptionEngine(item);
  const language = transcriptionLanguage(item);
  const detected = languages || meetingLanguages(item.detectedLanguages);
  const separation = separationValue(
    speakerSeparation(roster),
    retention.state === 'deleted',
    recordedSpeakerSeparation(item),
  );

  const items = [
    { label: 'Captured with', value: sourceValue(source, vp, platform) },
    ...(engine ? [{ label: 'Transcription engine', value: engine }] : []),
    ...(language ? [{ label: 'Transcription language', value: language }] : []),
    ...(detected.length
      ? [
          {
            label: detected.length === 1 ? 'Detected language' : 'Detected languages',
            value: detectedLanguagesText(detected),
          },
        ]
      : []),
    { label: 'Speaker separation', value: separation },
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
          description={
            hasRecordedCaptureContext(item)
              ? undefined
              : 'This meeting started before LMA recorded how meetings are captured: its source and speaker ' +
                'separation are inferred, and its transcription engine and language are not known.'
          }
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
  // The meeting's detected languages (meetingLanguages), when the caller has
  // read them from the transcript as well; the meeting record's otherwise.
  languages: PropTypes.arrayOf(PropTypes.string),
  // What useMeetingConsent returns.
  // eslint-disable-next-line react/forbid-prop-types
  consentState: PropTypes.object,
  onShowConsentRecord: PropTypes.func,
};

export default CaptureContextPanel;
