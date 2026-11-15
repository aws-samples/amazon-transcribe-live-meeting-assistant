/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Says, above the meeting's tabs, that transcription is paused because someone
 * typed "LMA pause" to the meeting's Virtual Participant in the meeting chat:
 * since when, by whom when the chat showed a sender, and that "LMA start" in
 * the meeting chat resumes it. Read from Call.TranscriptionPause, which the
 * Virtual Participant updates on each pause and resume; shown only while it
 * reads paused and the meeting is live, so it goes when transcription resumes
 * or the meeting ends. Dismissing it hides it until the next pause.
 *
 * Consent withholding capture is a different reason with its own alert
 * (ConsentPauseAlert); a meeting paused both ways shows both.
 */
import React, { useEffect, useState } from 'react';
import PropTypes from 'prop-types';
import { Alert } from '@cloudscape-design/components';

/** The command that resumes transcription, as typed in the meeting chat. */
export const RESUME_COMMAND = 'LMA start';

/**
 * Call.TranscriptionPause read: `{ paused, at, by }`, or null when it is absent
 * or unreadable (which reads as not paused).
 */
export const parseTranscriptionPause = (value) => {
  if (!value) return null;
  let state = value;
  if (typeof value === 'string') {
    try {
      state = JSON.parse(value);
    } catch {
      return null;
    }
  }
  if (!state || typeof state !== 'object' || typeof state.Paused !== 'boolean') return null;
  return {
    paused: state.Paused,
    at: typeof state.At === 'string' ? state.At : null,
    by: typeof state.By === 'string' && state.By.trim() ? state.By.trim() : null,
  };
};

const formatTime = (iso) => {
  const when = new Date(iso);
  return Number.isNaN(when.getTime()) ? iso : when.toLocaleString();
};

const TranscriptionPauseAlert = ({ isLive, transcriptionPause = undefined }) => {
  const pause = parseTranscriptionPause(transcriptionPause);
  const [dismissed, setDismissed] = useState(false);
  // A new pause (or a resume) shows the alert again after a dismissal.
  useEffect(() => setDismissed(false), [pause?.paused, pause?.at]);

  if (!isLive || !pause?.paused || dismissed) return null;

  const since = pause.at ? ` since ${formatTime(pause.at)}` : '';
  const by = pause.by ? `, by ${pause.by}` : '';
  return (
    <Alert
      type="warning"
      dismissible
      i18nStrings={{ dismissAriaLabel: 'Dismiss' }}
      onDismiss={() => setDismissed(true)}
      header="Transcription is paused"
    >
      {`Transcription was paused from the meeting chat${since}${by}. Nothing said in the meeting is ` +
        `transcribed until someone types "${RESUME_COMMAND}" in the meeting chat.`}
    </Alert>
  );
};

TranscriptionPauseAlert.propTypes = {
  isLive: PropTypes.bool.isRequired,
  // Call.TranscriptionPause as stored: a JSON string.
  transcriptionPause: PropTypes.string,
};

export default TranscriptionPauseAlert;
