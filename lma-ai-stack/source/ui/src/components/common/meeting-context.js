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
 * A meeting records how it was captured and transcribed when it starts:
 * CaptureSource, TranscriptionEngine, TranscriptionLanguage and
 * SpeakerSeparation on the meeting record, which these read first. A meeting
 * started before those were stored has none of them. For such a meeting the
 * source is inferred and said to be: the Virtual Participant writes fixed labels
 * on the record ('Virtual Participant', 'LMA System') and links its own record to
 * the meeting, and the other sources are told apart by the time stamp each
 * appends to the Meeting ID (see meeting-header.js) -- Stream Audio and the
 * browser extension append the same one, so they are named together. Its
 * engine and language are not known, and its speaker separation is read from
 * the voice labels in the transcript (speakerSeparation).
 */
import { STATUS_LABELS } from './meeting-consent';

export const SOURCE_VIRTUAL_PARTICIPANT = 'virtual-participant';
export const SOURCE_STREAM_AUDIO = 'stream-audio';
export const SOURCE_DESKTOP_APP = 'desktop-app';
export const SOURCE_UPLOAD = 'upload';
export const SOURCE_BROWSER_EXTENSION = 'browser-extension';
export const SOURCE_API = 'api';
export const SOURCE_UNKNOWN = 'unknown';

const SOURCE_LABELS = {
  [SOURCE_VIRTUAL_PARTICIPANT]: 'Virtual Participant',
  [SOURCE_STREAM_AUDIO]: 'Stream Audio or the browser extension',
  [SOURCE_DESKTOP_APP]: 'Desktop Capture App',
  [SOURCE_UPLOAD]: 'Uploaded recording',
  [SOURCE_UNKNOWN]: 'Not known (an MCP client, the API, or a Meeting ID typed by hand)',
};

// The values of the meeting record's CaptureSource, and how each reads.
const RECORDED_SOURCES = {
  VIRTUAL_PARTICIPANT: { kind: SOURCE_VIRTUAL_PARTICIPANT, label: 'Virtual Participant' },
  STREAM_AUDIO: { kind: SOURCE_STREAM_AUDIO, label: 'Stream Audio' },
  BROWSER_EXTENSION: { kind: SOURCE_BROWSER_EXTENSION, label: 'Browser extension' },
  DESKTOP_APP: { kind: SOURCE_DESKTOP_APP, label: 'Desktop Capture App' },
  UPLOAD: { kind: SOURCE_UPLOAD, label: 'Uploaded recording' },
  API: { kind: SOURCE_API, label: 'Another client of the streaming API (such as an MCP client)' },
};

const ENGINE_LABELS = { TRANSCRIBE: 'Amazon Transcribe', MICROVM_ASR: 'ASR MicroVM' };

const LANGUAGE_SETTINGS = {
  'identify-language': 'Identified by Amazon Transcribe (one language)',
  'identify-multiple-languages': 'Identified by Amazon Transcribe (several languages)',
};

const SEPARATION_LABELS = {
  BOTH: 'On for both channels',
  REMOTE: 'On for the meeting audio',
  LOCAL: 'On for the microphone',
  NONE: 'Off',
};

const recorded = (value) => (typeof value === 'string' && value.trim() ? value.trim() : null);

/** Whether the meeting record says how the meeting was captured (any of the four fields). */
export const hasRecordedCaptureContext = (item) =>
  [item?.captureSource, item?.transcriptionEngine, item?.transcriptionLanguage, item?.speakerSeparation].some(recorded);

// The Virtual Participant's own labels on the meeting record.
const VP_REMOTE_LABEL = 'Virtual Participant';
const VP_SYSTEM_LABEL = 'LMA System';

// The time stamps each source appends to the Meeting ID (see meeting-header.js).
const VP_SUFFIX = /-\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}-\d{3}$/;
const STREAM_AUDIO_SUFFIX = /\s+-\s+\d{4}-\d{2}-\d{2}-\d{2}:\d{2}:\d{2}\.\d{1,3}$/;
const DESKTOP_SUFFIX = /\s+-\s+\d{4}-\d{2}-\d{2}(?: \d{2}:\d{2}|T\d{2}:\d{2}:\d{2}Z?)$/;
const UPLOAD_SUFFIX = /-\d{8}T\d{6}-[0-9a-f]{8}$/;

/**
 * Where a meeting's audio came from: { kind, label, inferred }. The meeting
 * record's CaptureSource when it has one. Otherwise `vp`, the Virtual
 * Participant linked to the meeting, or its labels, and failing those the form
 * of the Meeting ID -- `inferred` is true for an answer read from that form.
 */
export const captureSource = (item, vp = null) => {
  const stored = recorded(item?.captureSource);
  if (stored) {
    const known = RECORDED_SOURCES[stored.toUpperCase()];
    return known ? { ...known, inferred: false } : { kind: SOURCE_UNKNOWN, label: stored, inferred: false };
  }
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

/** The engine that transcribed the meeting, as the record names it, or null when it does not. */
export const transcriptionEngine = (item) => {
  const stored = recorded(item?.transcriptionEngine);
  return stored ? ENGINE_LABELS[stored.toUpperCase()] || stored : null;
};

/**
 * The transcription language setting the meeting used, or null when the record
 * does not say: a language code, Amazon Transcribe's language identification,
 * or, for the ASR MicroVM, the model's own language. A meeting that moved to the
 * MicroVM after START can still hold the language START recorded; the engine
 * decides.
 */
export const transcriptionLanguage = (item) => {
  if (recorded(item?.transcriptionEngine)?.toUpperCase() === 'MICROVM_ASR') return 'Set by the ASR model';
  const stored = recorded(item?.transcriptionLanguage);
  return stored ? LANGUAGE_SETTINGS[stored.toLowerCase()] || stored : null;
};

/** Which channels speaker separation was asked for, as the record says, or null when it does not. */
export const recordedSpeakerSeparation = (item) => {
  const stored = recorded(item?.speakerSeparation);
  return stored ? SEPARATION_LABELS[stored.toUpperCase()] || stored : null;
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
