/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The Stream Audio consent step, without React.
 *
 * The WebSocket transcriber answers a CONSENT_SETTINGS frame with the Meeting
 * Consent settings in force; the page shows the notice, collects the user's
 * confirmation, and sends it in the `consent` field of START. The transcriber
 * records it on the meeting once the meeting exists and gates capture on the
 * record. See docs/meeting-consent.md.
 */

export const CONSENT_MODE_OFF = 'off';
export const CONSENT_MODE_NOTIFY = 'notify';
export const CONSENT_MODE_OPT_IN = 'opt_in';

export const CONSENT_METHODS = [
  { value: 'verbal', label: 'Out loud', description: 'I read the notice out and everyone heard it.' },
  { value: 'chat', label: 'In the meeting chat', description: 'I posted the notice in the meeting chat.' },
  {
    value: 'invite_terms',
    label: "In the invitation's terms",
    description: 'The meeting invitation said the meeting is recorded and transcribed.',
  },
];

/** The codes with which the transcriber refuses a START: no meeting is created. */
export const START_REFUSALS = new Set([
  'CONSENT_REQUIRED',
  'CONSENT_NOT_CONFIRMED',
  'CONSENT_RULE_UNSUPPORTED',
  'MEETING_NOT_OWNED',
]);

/**
 * Whether a CONSENT_STATE frame says the transcriber stopped capturing this
 * meeting. It only ever sends captureAllowed false when it has.
 */
export const captureStopped = (frame) => frame?.event === 'CONSENT_STATE' && frame.captureAllowed === false;

/**
 * The notice with its placeholders filled as the server fills them:
 * {LMA_USER}, {meeting}, {consent_phrase} and {objection_phrase}.
 */
export const renderNotice = (template, { lmaUser, meeting, consentPhrase, objectionPhrase }) => {
  const values = {
    '{LMA_USER}': lmaUser || 'The meeting organizer',
    '{meeting}': meeting || 'this meeting',
    '{consent_phrase}': consentPhrase || '',
    '{objection_phrase}': objectionPhrase || '',
  };
  return (template || '')
    .replace(/\{LMA_USER\}|\{meeting\}|\{consent_phrase\}|\{objection_phrase\}/g, (key) => values[key])
    .slice(0, 2000);
};

/** Whether the consent step applies: the server answered and its mode is not off. */
export const consentRequired = (settings) =>
  Boolean(settings?.available) && [CONSENT_MODE_NOTIFY, CONSENT_MODE_OPT_IN].includes(settings.mode);

/**
 * Why streaming may not start yet, or '' when it may. An opt-in meeting needs
 * the confirmation and its method; a notify meeting may start without them.
 */
export const startBlockedReason = (settings, step) => {
  if (!consentRequired(settings)) return '';
  if (settings.supported === false) return settings.reason || 'This deployment does not allow streaming here.';
  if (settings.mode === CONSENT_MODE_OPT_IN) {
    if (!step.confirmed) return 'Confirm that everyone agreed before you start.';
    if (!step.method) return 'Choose how everyone agreed.';
  }
  return '';
};

/** The `consent` field of START. */
export const consentPayload = (settings, step, { lmaUser, meetingName, noticeText }) => ({
  mode: settings.mode,
  confirmed: Boolean(step.confirmed),
  ...(step.method ? { method: step.method } : {}),
  noticeText,
  lmaUser,
  meetingName,
  ...(step.statement?.trim() ? { statement: step.statement.trim() } : {}),
  client: 'Stream Audio',
});

/** A JSON frame from the transcriber, or null for anything else (binary, unparseable). */
export const parseServerFrame = (data) => {
  if (typeof data !== 'string') return null;
  try {
    const frame = JSON.parse(data);
    return frame && typeof frame === 'object' && typeof frame.event === 'string' ? frame : null;
  } catch {
    return null;
  }
};

/** What the page says about the meeting's consent record from a CONSENT_STATE frame. */
export const describeConsentState = (frame) => {
  if (!frame) return null;
  if (frame.message) {
    return { type: frame.captureAllowed ? 'warning' : 'error', text: frame.message };
  }
  if (frame.evidence) {
    const verdicts = {
      supports_consent: 'the check found the notice and no objection',
      objection_found: 'the check found an objection',
      inconclusive: 'the check was inconclusive',
    };
    const verdict = verdicts[frame.evidence.verdict];
    return {
      type: frame.evidence.verdict === 'objection_found' ? 'warning' : 'success',
      text: `Evidence screenshot stored on the meeting${verdict ? `; ${verdict}` : ''}.`,
    };
  }
  if (frame.status === 'objected') return { type: 'error', text: 'A participant objected. Capture stopped.' };
  if (frame.captureAllowed) {
    return { type: 'success', text: `Consent recorded on the meeting (${frame.status}). Recording and transcribing.` };
  }
  return { type: 'info', text: `Consent ${frame.status || 'pending'}: not recording or transcribing yet.` };
};

/** Longest side of an evidence screenshot, in pixels: readable chat text, a modest upload. */
const MAX_EVIDENCE_SIDE = 1920;
/** Base64 characters the transcriber accepts for one image (3.75 MB of JPEG). */
const MAX_EVIDENCE_BASE64 = 5242880;

/**
 * One frame of a video track as a JPEG data URL, scaled to at most
 * MAX_EVIDENCE_SIDE on its longest side, at a lower quality if it is too large.
 */
export const frameToJpeg = async (track, documentRef = document) => {
  const video = documentRef.createElement('video');
  video.muted = true;
  video.playsInline = true;
  video.srcObject = new MediaStream([track]);
  await video.play();
  if (!video.videoWidth) {
    await new Promise((resolve) => {
      video.onloadeddata = resolve;
    });
  }
  const scale = Math.min(1, MAX_EVIDENCE_SIDE / Math.max(video.videoWidth, video.videoHeight));
  const canvas = documentRef.createElement('canvas');
  canvas.width = Math.round(video.videoWidth * scale);
  canvas.height = Math.round(video.videoHeight * scale);
  canvas.getContext('2d').drawImage(video, 0, 0, canvas.width, canvas.height);
  video.pause();
  video.srcObject = null;
  let quality = 0.85;
  let dataUrl = canvas.toDataURL('image/jpeg', quality);
  while (dataUrl.length > MAX_EVIDENCE_BASE64 && quality > 0.3) {
    quality -= 0.15;
    dataUrl = canvas.toDataURL('image/jpeg', quality);
  }
  return dataUrl;
};

/**
 * Ask the user to pick the meeting's tab or window and keep one frame of it.
 * The capture is stopped as soon as the frame is taken.
 */
export const captureEvidenceScreenshot = async (mediaDevices = navigator.mediaDevices) => {
  const stream = await mediaDevices.getDisplayMedia({ video: true, audio: false });
  try {
    const [track] = stream.getVideoTracks();
    if (!track) throw new Error('The shared surface has no video.');
    return await frameToJpeg(track);
  } finally {
    stream.getTracks().forEach((t) => t.stop());
  }
};
