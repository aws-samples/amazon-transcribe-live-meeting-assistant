/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

// Join again: the Create Virtual Participant form, filled in from an existing
// Virtual Participant (the source), with a choice at the top:
//
// - Continue the source's meeting: the same Virtual Participant rejoins and
//   appends to the same LMA meeting (rejoinVirtualParticipant). It joins now,
//   with the source's saved settings.
// - Start a new meeting: a new Virtual Participant and a new LMA meeting
//   (joinAgainVirtualParticipant). For the same meeting, an empty password
//   field means the source's saved password, which the server copies.
//
// The Virtual Participant page builds the values and opens the list page's
// create form with them in the router state. The server holds the rules (the
// source's owner or an Admin; for Continue, also the meeting's owner or an
// Admin); this module only offers what they allow, and says why Continue is
// not offered. See docs/virtual-participant.md#join-again.

import { consentChoiceOf } from './scheduled-edit';
import { EMPTY_VP_FORM, submittedMeetingFields } from './vp-form';

/** The router state key that carries the values to the list page's create form. */
export const JOIN_AGAIN_STATE_KEY = 'joinAgainFrom';

export const JOIN_MODE_CONTINUE = 'continue';
export const JOIN_MODE_NEW = 'new';

/** The statuses of a Virtual Participant that has left its meeting, which rejoin accepts. */
export const REJOINABLE_STATUSES = ['ENDED', 'COMPLETED', 'FAILED'];

/** Shown on the password field when the source's saved password is used. */
export const SAVED_PASSWORD_HINT = 'The saved password will be used.';
export const SAVED_PASSWORD_NEW_HINT = 'The saved password will be used. Enter a password to use a different one.';

/** Shown when an edit of the meeting moved the choice to Start a new meeting. */
export const MEETING_CHANGED_NOTE =
  'You changed the meeting, so this starts a new meeting. Choose Continue to go back to the original meeting details.';

export const CONTINUE_SETTINGS_NOTE =
  "Continuing joins now with the Virtual Participant's saved settings. " +
  'To change them, or to schedule, start a new meeting.';

export const NO_MEETING_REASON =
  'There is no LMA meeting to continue: the Virtual Participant never started capturing one, or it has been deleted.';
export const TRANSCRIPT_DELETED_REASON = "The meeting's transcript has been deleted, so it cannot be continued.";

export const rejoinVirtualParticipantMutation = /* GraphQL */ `
  mutation RejoinVirtualParticipant($input: RejoinVirtualParticipantInput!) {
    rejoinVirtualParticipant(input: $input) {
      id
      status
      errorMessage
      updatedAt
      owner
      Owner
      SharedWith
      CallId
      vncReady
      sessionNumber
      rejoinedAt
    }
  }
`;

export const joinAgainVirtualParticipantMutation = /* GraphQL */ `
  mutation JoinAgainVirtualParticipant($input: JoinAgainVirtualParticipantInput!) {
    joinAgainVirtualParticipant(input: $input) {
      id
      meetingName
      meetingPlatform
      meetingId
      status
      Owner
      SharedWith
      createdAt
    }
  }
`;

/** The source's meeting record, as far as Continue needs it. */
export const joinAgainMeetingQuery = /* GraphQL */ `
  query GetCallForJoinAgain($callId: ID!) {
    getCall(CallId: $callId) {
      CallId
      Status
      TranscriptDeletedAt
    }
  }
`;

/**
 * Whether the page offers Join again: to the Virtual Participant's owner and
 * Admins (`canManage`), once it is no longer waiting for its schedule.
 */
export const canJoinAgain = (vp, canManage) => !!canManage && !!vp && !!vp.meetingId && vp.status !== 'SCHEDULED';

/**
 * Whether Continue is available, and why not, from the source and what reading
 * its meeting returned: `meeting` is the record, null when there is none the
 * viewer can read, or undefined when it could not be read (the server then
 * decides when the form is submitted).
 */
export const continueOptionFor = (vp, meeting) => {
  if (!vp?.CallId) {
    return { available: false, reason: 'This Virtual Participant never started a meeting.' };
  }
  if (!REJOINABLE_STATUSES.includes(vp.status)) {
    return {
      available: false,
      reason: 'The Virtual Participant is still in the meeting. It can continue it once it has left.',
    };
  }
  if (meeting === null) return { available: false, reason: NO_MEETING_REASON };
  if (meeting?.TranscriptDeletedAt) return { available: false, reason: TRANSCRIPT_DELETED_REASON };
  return { available: true, reason: '' };
};

/**
 * The meeting record for continueOptionFor, from a getCall answer or error:
 * null when there is no meeting the viewer may read (getCall refuses one that
 * does not exist as it refuses one that is not theirs), undefined when the read
 * failed otherwise.
 */
export const meetingFromRead = ({ data = undefined, error = undefined } = {}) => {
  if (error) {
    const first = error?.errors?.[0] || {};
    const refused =
      first.errorType === 'AuthorizationError' || /not authorized/i.test(first.message || error?.message || '');
    return refused ? null : undefined;
  }
  return data?.getCall || null;
};

/**
 * The values Join again opens the create form with.
 *
 * Copied: platform, meeting ID or link, meeting name, agenda, consent choice
 * and video recording. Left as for a new Virtual Participant: the date and
 * time, the password (the API never returns it; the server uses the saved one
 * for the same meeting), the Zoom account choice and the browser profile (both
 * belong to the user who launches), and the acknowledgement checkbox.
 * `continueOption` comes from continueOptionFor.
 */
export const joinAgainFormFrom = (vp, continueOption = { available: false, reason: '' }) => {
  const form = {
    ...EMPTY_VP_FORM,
    meetingName: vp?.meetingName || '',
    meetingPlatform: vp?.meetingPlatform || EMPTY_VP_FORM.meetingPlatform,
    meetingId: vp?.meetingId || '',
    agenda: vp?.agenda || '',
  };
  return {
    form,
    consentChoice: consentChoiceOf(vp || {}),
    // Absent on rows the form did not write: the launch default is on.
    enableVideoRecording: vp?.enableVideoRecording !== false,
    source: {
      id: vp?.id || '',
      meetingName: form.meetingName,
      meetingPlatform: form.meetingPlatform,
      meetingId: form.meetingId,
      hasMeetingPassword: vp?.hasMeetingPassword === true,
    },
    continueOption,
    mode: continueOption.available ? JOIN_MODE_CONTINUE : JOIN_MODE_NEW,
  };
};

/** The values Join again left in the router state, or null. */
export const joinAgainFromState = (state) => {
  const values = state?.[JOIN_AGAIN_STATE_KEY];
  return values && values.form && values.source ? values : null;
};

/** Whether the form names another meeting than the source: another platform, meeting ID or link. */
export const meetingChanged = (source, form) =>
  source.meetingPlatform !== form.meetingPlatform ||
  submittedMeetingFields(source).meetingId !== submittedMeetingFields(form).meetingId;

/**
 * A change to the form, in a Join again form: `{ form, mode, changedNote }`.
 * In Continue, an edit that names another meeting moves the choice to Start a
 * new meeting, with a note saying so.
 */
export const joinAgainFormChange = ({ mode, source, form, changedNote = false }, patch) => {
  const next = { ...form, ...patch };
  if (mode === JOIN_MODE_CONTINUE && meetingChanged(source, next)) {
    return { form: next, mode: JOIN_MODE_NEW, changedNote: true };
  }
  return { form: next, mode, changedNote: changedNote && meetingChanged(source, next) };
};

/**
 * The choice changed, in a Join again form `values` (joinAgainFormFrom) whose
 * fields are now `form`: `{ form, mode, changedNote }`, and for Continue also
 * `consentChoice` and `enableVideoRecording`. Continue restores the source's
 * meeting details and saved settings, the ones it joins with, and empties the
 * password field (Continue uses the saved password).
 */
export const joinAgainModeChange = (values, form, mode) => {
  if (mode !== JOIN_MODE_CONTINUE) return { form, mode, changedNote: false };
  return {
    form: { ...values.form },
    consentChoice: values.consentChoice,
    enableVideoRecording: values.enableVideoRecording,
    mode,
    changedNote: false,
  };
};

/**
 * The joinAgainVirtualParticipant input for the create form's `vpInput` (the
 * createVirtualParticipant input the form builds). The server sets the status,
 * the owner and the launching user's sub, and stores the Zoom account choice
 * as `useStoredZoomAccount`. An empty password is sent as none, so the server
 * can use the saved one for the same meeting.
 */
export const joinAgainInputFrom = (sourceVpId, vpInput, { useStoredZoomAccount = false } = {}) => {
  const input = { sourceVpId };
  [
    'meetingName',
    'meetingPlatform',
    'meetingId',
    'meetingTime',
    'scheduledTimeZone',
    'enableVideoRecording',
    'agenda',
    'consentMode',
    'consentRule',
  ].forEach((field) => {
    if (vpInput[field] !== undefined && vpInput[field] !== null) input[field] = vpInput[field];
  });
  if (vpInput.meetingPassword) input.meetingPassword = vpInput.meetingPassword;
  if (vpInput.meetingPlatform === 'ZOOM') input.useStoredZoomAccount = !!useStoredZoomAccount;
  return input;
};

/** The message an AppSync error carries, or a general one. */
export const joinAgainErrorText = (error, fallback) => error?.errors?.[0]?.message || error?.message || fallback;

/**
 * Whether the server will use the source's saved password: the source had one,
 * the form names the same meeting, the meeting takes a password, and none is
 * typed. join_again.py holds the same rule.
 */
export const savedPasswordApplies = (source, form) =>
  !!source?.hasMeetingPassword &&
  !meetingChanged(source, form) &&
  submittedMeetingFields(form).passwordApplies &&
  !form.meetingPassword;

/** The password field's description in a Join again form. */
export const joinAgainPasswordHint = (mode, source, form) => {
  if (!source?.hasMeetingPassword || meetingChanged(source, form)) return '';
  if (mode === JOIN_MODE_CONTINUE) return SAVED_PASSWORD_HINT;
  return savedPasswordApplies(source, form) ? SAVED_PASSWORD_NEW_HINT : '';
};

/** The Continue choice's label. */
export const continueLabel = (source) =>
  source?.meetingName ? `Continue meeting "${source.meetingName}"` : 'Continue the meeting';
