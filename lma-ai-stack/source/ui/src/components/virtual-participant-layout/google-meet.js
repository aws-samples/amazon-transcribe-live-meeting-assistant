/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

// Google Meet meeting codes, as the Create Virtual Participant form accepts
// them. The same rule as parseGoogleMeetMeeting in the Virtual Participant
// (lma-virtual-participant-stack/backend/src/google-meet-page.ts), the invitation
// parser, the SDK and the meeting assistant's tools, each of which runs the shared
// table in test-fixtures/google-meet-inputs.json: a bare code in any case, with or
// without hyphens, or a meet.google.com link to one, including the /meet/<code>
// form Meet redirects to. A /lookup/ nickname link is refused, as everywhere else:
// only signed-in members of the organiser's Workspace can open one. The form
// stores the code.

const MEET_CODE = /^([a-z]{3})-?([a-z]{4})-?([a-z]{3})$/;
const MEET_LINK = /^(?:https?:\/\/)?meet\.google\.com\/(?:meet\/)?([a-z]{3}-?[a-z]{4}-?[a-z]{3})\/?(?:[?#].*)?$/;

/** The meeting code ("abc-defg-hij") in a Meet code or link, or null when it is neither. */
export const googleMeetCode = (value) => {
  const text = String(value || '')
    .replace(/\s+/g, '')
    .toLowerCase();
  if (!text) return null;
  const link = MEET_LINK.exec(text);
  const code = MEET_CODE.exec(link ? link[1] : text);
  return code ? `${code[1]}-${code[2]}-${code[3]}` : null;
};

/** The form's error text for a Meet meeting ID, or '' when there is nothing to say yet. */
export const googleMeetIdError = (value) => {
  if (!String(value || '').trim()) return '';
  if (googleMeetCode(value)) return '';
  if (/meet\.google\.com\/lookup\//i.test(String(value).replace(/\s+/g, ''))) {
    return (
      'A meet.google.com/lookup/ link is a meeting nickname, which only signed-in members of the ' +
      "organiser's Google Workspace can open. Enter the meeting code (for example abc-defg-hij)."
    );
  }
  return 'Enter the meeting code (for example abc-defg-hij) or a https://meet.google.com/ link.';
};
