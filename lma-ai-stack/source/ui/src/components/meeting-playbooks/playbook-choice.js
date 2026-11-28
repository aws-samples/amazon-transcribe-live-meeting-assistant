/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
// The meeting playbook a Virtual Participant form chooses for the meeting it
// creates (docs/meeting-playbooks.md), as guidance-options.js does for live
// guidance. "default" leaves it to the configuration profile's default
// playbook, applied when the meeting starts; "none" is no playbook whatever the
// default; any other value is a playbook's skill id. Stored on the VP row as
// `playbook`.

export const DEFAULT_CHOICE = 'default';
export const NO_PLAYBOOK = 'none';

/** The form's choice for a stored row. */
export const playbookChoiceOf = (vp) => (vp?.playbook ? String(vp.playbook) : DEFAULT_CHOICE);

/** The createVirtualParticipant (or joinAgain) fields for a choice: nothing for the default. */
export const playbookCreateInput = (choice) => (!choice || choice === DEFAULT_CHOICE ? {} : { playbook: choice });

/**
 * The updateScheduledVirtualParticipant fields for a choice on a stored row:
 * nothing when it is unchanged, and null to go back to the profile's default.
 */
export const playbookEditInput = (choice, vp) => {
  if ((choice || DEFAULT_CHOICE) === playbookChoiceOf(vp)) return {};
  if (!choice || choice === DEFAULT_CHOICE) return { playbook: null };
  return { playbook: choice };
};

/** How a Virtual Participant's page names a stored row's choice. */
export const describePlaybookChoice = (vp, playbooks = []) => {
  const choice = playbookChoiceOf(vp);
  if (choice === NO_PLAYBOOK) return 'No playbook';
  if (choice === DEFAULT_CHOICE) return "The configuration profile's default";
  return playbooks.find((p) => p.SkillId === choice)?.DisplayName || choice;
};

/** The options of the form's select, the profile's default named when it has one. */
export const playbookSelectOptions = (options) => {
  const playbooks = options?.Playbooks || [];
  const fallback = playbooks.find((p) => p.SkillId === options?.ProfileDefault);
  return [
    {
      value: DEFAULT_CHOICE,
      label: fallback ? `The profile's default (${fallback.DisplayName})` : "The profile's default (none)",
    },
    { value: NO_PLAYBOOK, label: 'No playbook' },
    ...playbooks.map((p) => ({ value: p.SkillId, label: p.DisplayName, description: p.Description })),
  ];
};
