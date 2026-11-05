/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * What the meeting page says about how a meeting was captured and processed:
 * where its audio came from, whether its voices were told apart, the consent
 * setting that applied to it, and what happens to its transcript. Read from
 * what the meeting record and the page already hold; kept free of React so it
 * can be tested directly.
 *
 * The meeting record does not say which client captured it. The Virtual
 * Participant writes fixed labels on it ('Virtual Participant', 'LMA System')
 * and links its own record to the meeting; the other sources are told apart by
 * the time stamp each appends to the Meeting ID (see meeting-header.js). Stream
 * Audio and the browser extension append the same one, so they are named
 * together. The transcription engine, the transcription language and whether
 * speaker separation was asked for are not stored on the meeting at all.
 */
import { STATUS_LABELS } from './meeting-consent';

export const SOURCE_VIRTUAL_PARTICIPANT = 'virtual-participant';
export const SOURCE_STREAM_AUDIO = 'stream-audio';
export const SOURCE_DESKTOP_APP = 'desktop-app';
export const SOURCE_UPLOAD = 'upload';
export const SOURCE_UNKNOWN = 'unknown';

const SOURCE_LABELS = {
  [SOURCE_VIRTUAL_PARTICIPANT]: 'Virtual Participant',
  [SOURCE_STREAM_AUDIO]: 'Stream Audio or the browser extension',
  [SOURCE_DESKTOP_APP]: 'Desktop Capture App',
  [SOURCE_UPLOAD]: 'Uploaded recording',
  [SOURCE_UNKNOWN]: 'Not known (an MCP client, the API, or a Meeting ID typed by hand)',
};

// The Virtual Participant's own labels on the meeting record.
const VP_REMOTE_LABEL = 'Virtual Participant';
const VP_SYSTEM_LABEL = 'LMA System';

// The time stamps each source appends to the Meeting ID (see meeting-header.js).
const VP_SUFFIX = /-\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}-\d{3}$/;
const STREAM_AUDIO_SUFFIX = /\s+-\s+\d{4}-\d{2}-\d{2}-\d{2}:\d{2}:\d{2}\.\d{1,3}$/;
const DESKTOP_SUFFIX = /\s+-\s+\d{4}-\d{2}-\d{2}(?: \d{2}:\d{2}|T\d{2}:\d{2}:\d{2}Z?)$/;
const UPLOAD_SUFFIX = /-\d{8}T\d{6}-[0-9a-f]{8}$/;

/**
 * Where a meeting's audio came from: { kind, label, inferred }. `vp` is the
 * Virtual Participant linked to the meeting, when one is. `inferred` is true
 * when the answer is read from the Meeting ID's form rather than recorded.
 */
export const captureSource = (item, vp = null) => {
  const callId = typeof item?.callId === 'string' ? item.callId : '';
  const vpLabels = item?.remoteParticipantName === VP_REMOTE_LABEL || item?.systemLabel === VP_SYSTEM_LABEL;
  if (vp || vpLabels) {
    return { kind: SOURCE_VIRTUAL_PARTICIPANT, label: SOURCE_LABELS[SOURCE_VIRTUAL_PARTICIPANT], inferred: false };
  }
  let kind = SOURCE_UNKNOWN;
  if (UPLOAD_SUFFIX.test(callId)) kind = SOURCE_UPLOAD;
  else if (STREAM_AUDIO_SUFFIX.test(callId)) kind = SOURCE_STREAM_AUDIO;
  else if (DESKTOP_SUFFIX.test(callId) || callId.startsWith('LMA native prototype')) kind = SOURCE_DESKTOP_APP;
  else if (VP_SUFFIX.test(callId)) kind = SOURCE_VIRTUAL_PARTICIPANT;
  return { kind, label: SOURCE_LABELS[kind], inferred: kind !== SOURCE_UNKNOWN };
};

/**
 * Whether the meeting's voices were told apart, from its transcript: the
 * number of voices the speech engine labelled (spk_0, spk_1, ...) and whether
 * any channel holds more than one of them. Null when the transcript holds no
 * speech to read it from. `roster` is buildSpeakerRoster's.
 */
export const speakerSeparation = (roster) => {
  const rows = roster || [];
  if (!rows.length) return null;
  const labelled = rows.filter((row) => row.voiceId);
  const perChannel = {};
  labelled.forEach((row) => {
    perChannel[row.channel] = (perChannel[row.channel] || 0) + 1 + (row.mergedKeys?.length || 0);
  });
  return {
    separated: labelled.length > 0,
    voices: labelled.reduce((total, row) => total + 1 + (row.mergedKeys?.length || 0), 0),
    splitChannel: Object.values(perChannel).some((count) => count > 1),
  };
};

const MODE_SHORT = { off: 'Off', notify: 'Notify', opt_in: 'Opt-in' };

/** Whether a consent mode is one that is enforced: notify or opt-in. */
export const consentEnforced = (mode) => mode === 'notify' || mode === 'opt_in';

/**
 * The consent setting that applied to the meeting, for the header and the
 * Details tab: { known, enforced, mode, modeLabel, status, statusLabel, text,
 * captureAllowed }. `state` is what useMeetingConsent returns. Nothing is
 * known until the record (or the deployment's settings) has been read; a
 * meeting with no record, or one in mode off, had consent enforcement off.
 */
export const consentSummary = (state) => {
  const consent = state?.consent || null;
  const known = Boolean(consent || state?.settings);
  if (!consent || !consentEnforced(consent.Mode)) {
    return {
      known,
      enforced: false,
      mode: consent?.Mode || 'off',
      modeLabel: MODE_SHORT.off,
      text: 'Consent enforcement off',
      captureAllowed: true,
    };
  }
  const statusLabel = (STATUS_LABELS[consent.Status] || consent.Status || 'unknown').toLowerCase();
  const captureAllowed = state?.captureAllowed === false ? false : consent.CaptureAllowed !== false;
  return {
    known: true,
    enforced: true,
    mode: consent.Mode,
    modeLabel: MODE_SHORT[consent.Mode],
    status: consent.Status,
    statusLabel,
    text: `${MODE_SHORT[consent.Mode]} · ${statusLabel}`,
    captureAllowed,
  };
};

/** How long after a meeting starts a missing consent is called out on the page. */
export const CONSENT_PAUSE_GRACE_MS = 30000;

/**
 * When the page should say that transcription is paused for consent, in
 * milliseconds since the epoch, or null when it should not: a live meeting
 * whose consent is enforced and does not allow capture, from
 * CONSENT_PAUSE_GRACE_MS after the meeting (or its consent, if later) started.
 */
export const consentPauseDueAt = ({ isLive, summary, startedAt, consentStartedAt }) => {
  if (!isLive || !summary?.enforced || summary.captureAllowed) return null;
  const starts = [startedAt, consentStartedAt].map((iso) => Date.parse(iso || '')).filter((ms) => !Number.isNaN(ms));
  const start = starts.length ? Math.max(...starts) : Date.now();
  return start + CONSENT_PAUSE_GRACE_MS;
};

/**
 * What happens to the meeting's transcript under the deployment's Retention
 * settings, from what the meeting record says: { state, at, segments, files,
 * recordExpiresAt }. `state` is 'deleted' (deleted after the summary, at
 * `at`), 'scheduled' (to be deleted at `at`), or 'kept' (each segment expires
 * by the deployment's days to keep transcript segments). `recordExpiresAt` is
 * when the meeting record itself expires, when the record says.
 */
export const transcriptRetention = (item) => {
  const expires = Number(item?.expiresAfter);
  const recordExpiresAt = Number.isFinite(expires) && expires > 0 ? new Date(expires * 1000).toISOString() : null;
  if (item?.transcriptDeletedAt) {
    return {
      state: 'deleted',
      at: item.transcriptDeletedAt,
      segments: item.transcriptSegmentsDeleted ?? null,
      files: item.transcriptFilesDeleted ?? null,
      recordExpiresAt,
    };
  }
  if (item?.transcriptDeletionScheduledFor) {
    return { state: 'scheduled', at: item.transcriptDeletionScheduledFor, recordExpiresAt };
  }
  return { state: 'kept', at: null, recordExpiresAt };
};
