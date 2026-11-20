/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

// The live guidance choices a Virtual Participant form offers for the meeting
// it creates (docs/live-guidance.md). "default" leaves the choice to the
// deployment's Meeting Assistant › Live guidance › On for new meetings setting,
// which applies when the meeting starts; "on" and "off" are stored on the VP
// row as guidanceEnabled.
export const GUIDANCE_OPTIONS = [
  { value: 'default', label: "The deployment's setting" },
  { value: 'on', label: 'On: offer guidance while the meeting is in progress' },
  { value: 'off', label: 'Off' },
];

/** The form's choice for a stored row. */
export const guidanceChoiceOf = (vp) => {
  if (vp?.guidanceEnabled === true) return 'on';
  if (vp?.guidanceEnabled === false) return 'off';
  return 'default';
};

/** The createVirtualParticipant (or joinAgain) fields for a choice: nothing for the default. */
export const guidanceCreateInput = (choice) => {
  if (choice === 'on') return { guidanceEnabled: true };
  if (choice === 'off') return { guidanceEnabled: false };
  return {};
};

/**
 * The updateScheduledVirtualParticipant fields for a choice on a stored row:
 * nothing when it is unchanged, and null to go back to the deployment's setting.
 */
export const guidanceEditInput = (choice, vp) => {
  if (choice === guidanceChoiceOf(vp)) return {};
  if (choice === 'on') return { guidanceEnabled: true };
  if (choice === 'off') return { guidanceEnabled: false };
  return { guidanceEnabled: null };
};

/** How the Virtual Participant's page describes a stored row's choice. */
export const describeGuidance = (vp) => {
  const choice = guidanceChoiceOf(vp);
  if (choice === 'on') return 'On';
  if (choice === 'off') return 'Off';
  return "The deployment's setting";
};
