/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

// The meeting consent choices the Create Virtual Participant form offers for
// one meeting (docs/meeting-consent.md). "default" sends nothing, so the
// deployment's Meeting Consent setting applies; the others are stored on the VP
// row. Whether the deployment allows a choice is decided when the VP starts: by
// the VP before it joins, and again by the meeting consent resolver.
export const MEETING_CONSENT_OPTIONS = [
  { value: 'default', label: "The deployment's setting" },
  { value: 'off', label: 'Off' },
  { value: 'notify', label: 'Notify: post a notice; any objection stops recording' },
  { value: 'opt_in:each_participant', label: 'Opt-in: wait until every participant consents' },
  { value: 'opt_in:owner_attests', label: 'Opt-in: wait until I confirm that everyone agreed' },
];

const EACH_PARTICIPANT = 'opt_in:each_participant';

// What the Virtual Participant can see on each platform decides whether the
// each-participant rule can be met there (docs/meeting-consent.md, "What each
// platform supports"). Webex shows no attendee count, so it never can. The
// Teams browser join shows no chat sender; the Teams join through Azure
// Communication Services does, and which one a VP uses is decided by the
// deployment, so the option stays available for Teams with a note.
export const meetingConsentOptionsFor = (platform) =>
  MEETING_CONSENT_OPTIONS.map((option) => {
    if (option.value !== EACH_PARTICIPANT) return option;
    if (platform === 'WEBEX') {
      return {
        ...option,
        disabled: true,
        description:
          'Not available on Webex, which does not show who is present. ' +
          'Use "Opt-in: wait until I confirm that everyone agreed".',
      };
    }
    if (platform === 'TEAMS') {
      return {
        ...option,
        description:
          'Needs the Teams join through Azure Communication Services; the Teams browser join cannot meet it.',
      };
    }
    return option;
  });

/** Whether `choice` can be offered for `platform`; a choice that cannot falls back to the default. */
export const meetingConsentChoiceAvailable = (choice, platform) =>
  !meetingConsentOptionsFor(platform).some((option) => option.value === choice && option.disabled);

/** The createVirtualParticipant fields for one consent choice. */
export const meetingConsentInput = (choice) => {
  if (!choice || choice === 'default') return {};
  const [consentMode, consentRule] = choice.split(':');
  return consentRule ? { consentMode, consentRule } : { consentMode };
};
