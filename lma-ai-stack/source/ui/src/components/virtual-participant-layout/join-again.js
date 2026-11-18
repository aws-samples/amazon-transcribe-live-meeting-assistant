/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

// Join again: the Create Virtual Participant form, filled in from an existing
// Virtual Participant's meeting details, so a new Virtual Participant (and a
// new LMA meeting) can be sent to the same meeting. The Virtual Participant
// page builds the values and opens the list page's create form with them in
// the router state, so the new Virtual Participant is created by the same
// handler as any other.

import { consentChoiceOf } from './scheduled-edit';
import { EMPTY_VP_FORM, submittedMeetingFields } from './vp-form';

/** The router state key that carries the values to the list page's create form. */
export const JOIN_AGAIN_STATE_KEY = 'joinAgainFrom';

/** Shown on the meeting password field when the earlier Virtual Participant had one. */
export const PASSWORD_NOT_COPIED_HINT =
  "The earlier Virtual Participant's meeting password is not copied. Enter it again if the meeting needs one.";

/** Whether the page offers Join again: once the Virtual Participant is no longer waiting for its schedule. */
export const canJoinAgain = (vp) => !!vp && !!vp.meetingId && vp.status !== 'SCHEDULED';

/**
 * The create form's values for a new Virtual Participant in `vp`'s meeting.
 *
 * Copied: platform, meeting ID or link, meeting name, agenda, consent choice
 * and video recording. Left as for a new Virtual Participant: the date and
 * time (the user chooses Join Now or a time), the password (the API never
 * returns it; `passwordNotCopied` says the earlier one had one, so the form
 * can ask for it), the Zoom account choice and the browser profile (both
 * belong to the user who launches, not to the earlier Virtual Participant),
 * and the acknowledgement checkbox.
 */
export const joinAgainFormFrom = (vp) => {
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
    passwordNotCopied: vp?.hasMeetingPassword === true && submittedMeetingFields(form).passwordApplies,
  };
};

/** The values Join again left in the router state, or null. */
export const joinAgainFromState = (state) => {
  const values = state?.[JOIN_AGAIN_STATE_KEY];
  return values && values.form ? values : null;
};
