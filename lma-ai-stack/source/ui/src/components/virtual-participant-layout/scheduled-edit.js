/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

// Editing a scheduled Virtual Participant: who may, the form pre-filled from
// the stored row, and the updateScheduledVirtualParticipant input for what
// changed.

import { meetingConsentInput } from './meeting-consent-options';
import {
  EDIT_MIN_LEAD_MINUTES,
  browserTimeZone,
  formDateAndTime,
  formMeetingTimestamp,
  submittedMeetingFields,
} from './vp-form';

/**
 * Whether the page offers to edit the Virtual Participant: it is waiting for a
 * scheduled launch, and the viewer is its owner or an Admin, the users the
 * resolver lets make the change.
 */
export const canEditScheduled = (vp, { username, isAdmin }) =>
  !!vp && vp.status === 'SCHEDULED' && !!vp.isScheduled && (!!isAdmin || (!!username && vp.Owner === username));

/** Whether the meeting is still far enough away for a change to be accepted. */
export const editWindowOpen = (vp, now = Date.now()) => {
  const seconds = Number(vp?.meetingTime);
  return Number.isFinite(seconds) && seconds * 1000 - now >= EDIT_MIN_LEAD_MINUTES * 60 * 1000;
};

/** The form's consent choice for a stored consent mode and rule. */
export const consentChoiceOf = ({ consentMode, consentRule }) => {
  if (!consentMode) return 'default';
  return consentRule ? `${consentMode}:${consentRule}` : consentMode;
};

/** The form values for a stored row. The password field starts empty: empty keeps the current one. */
export const editFormFrom = (vp) => ({
  form: {
    meetingName: vp.meetingName || '',
    meetingPlatform: vp.meetingPlatform || 'ZOOM',
    meetingId: vp.meetingId || '',
    meetingPassword: '',
    ...formDateAndTime(vp.meetingTime),
    agenda: vp.agenda || '',
  },
  consentChoice: consentChoiceOf(vp),
  // Absent on rows the form did not write: the launch default is on.
  enableVideoRecording: vp.enableVideoRecording !== false,
  useZoomLogin: vp.useStoredZoomAccount === true,
});

/**
 * The updateScheduledVirtualParticipant input for the fields that differ from
 * the stored row, or null when nothing changed. `zoomChoiceOffered` says
 * whether the form offered the Zoom account choice.
 */
export const scheduledEditInput = (
  vp,
  { form, consentChoice, enableVideoRecording, useZoomLogin },
  { zoomChoiceOffered },
) => {
  const input = {};
  const { meetingId, meetingPassword, passwordApplies } = submittedMeetingFields(form);

  if (form.meetingName !== (vp.meetingName || '')) input.meetingName = form.meetingName;
  if (form.meetingPlatform !== (vp.meetingPlatform || 'ZOOM')) input.meetingPlatform = form.meetingPlatform;
  if (meetingId !== (vp.meetingId || '')) input.meetingId = meetingId;
  if (!passwordApplies && vp.hasMeetingPassword) {
    input.clearMeetingPassword = true;
  } else if (passwordApplies && meetingPassword) {
    input.meetingPassword = meetingPassword;
  }

  // Compared as the form shows it, to the minute, so an untouched time is not resent.
  const storedTime = formDateAndTime(vp.meetingTime);
  const meetingTime = formMeetingTimestamp(form.meetingDate, form.meetingTime);
  if (
    meetingTime !== null &&
    (form.meetingDate !== storedTime.meetingDate || form.meetingTime !== storedTime.meetingTime)
  ) {
    input.meetingTime = meetingTime;
    const timeZone = browserTimeZone();
    if (timeZone) input.scheduledTimeZone = timeZone;
  }

  const agenda = (form.agenda || '').trim();
  if (agenda !== (vp.agenda || '').trim()) input.agenda = agenda;

  if (consentChoice !== consentChoiceOf(vp)) {
    const consent = meetingConsentInput(consentChoice);
    input.consentMode = consent.consentMode || '';
    if (consent.consentRule) input.consentRule = consent.consentRule;
  }

  if (enableVideoRecording !== (vp.enableVideoRecording !== false)) {
    input.enableVideoRecording = enableVideoRecording;
  }

  const storedZoom = vp.useStoredZoomAccount === true;
  if (form.meetingPlatform === 'ZOOM' && zoomChoiceOffered) {
    if (useZoomLogin !== storedZoom) input.useStoredZoomAccount = useZoomLogin;
  } else if (storedZoom) {
    // Not a Zoom meeting any more, or no longer offered: join as a guest.
    input.useStoredZoomAccount = false;
  }

  return Object.keys(input).length ? { id: vp.id, ...input } : null;
};
