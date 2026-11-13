/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

// The meeting consent choices the Create Virtual Participant form offers for
// one meeting (docs/meeting-consent.md). "default" sends nothing, so the
// deployment's Meeting Consent setting applies; the others are stored on the VP
// row. Whether the deployment allows a choice is decided when the VP starts: by
// the VP before it joins, and again by the meeting consent resolver. The form
// applies the same rule ahead of them (meetingConsentChoicePermitted) when it
// could read the deployment's settings through getMeetingConsentDefaults.
export const MEETING_CONSENT_OPTIONS = [
  { value: 'default', label: "The deployment's setting" },
  { value: 'off', label: 'Off' },
  { value: 'notify', label: 'Notify: post a notice; any objection stops recording' },
  { value: 'opt_in:each_participant', label: 'Opt-in: wait until every participant consents' },
  { value: 'opt_in:owner_attests', label: 'Opt-in: wait until I confirm that everyone agreed' },
];

const EACH_PARTICIPANT = 'opt_in:each_participant';

/** The createVirtualParticipant fields for one consent choice. */
export const meetingConsentInput = (choice) => {
  if (!choice || choice === 'default') return {};
  const [consentMode, consentRule] = choice.split(':');
  return consentRule ? { consentMode, consentRule } : { consentMode };
};

// In order from least to most strict, as lma_consent.rules ranks them
// (lma-ai-stack/source/lambda_layers/configuration_layer/lma_consent/rules.py:
// MODES, RULES, strictness and override_permitted; the VP's copy is
// strictness and planConsent in lma-virtual-participant-stack/backend/src).
const CONSENT_MODES = ['off', 'notify', 'opt_in'];
const CONSENT_RULES = ['owner_attests', 'each_participant'];

/** How strict a mode and rule are, as lma_consent.rules.strictness ranks them. */
export const consentStrictness = (mode, rule) => [
  Math.max(0, CONSENT_MODES.indexOf(mode)),
  mode === 'opt_in' && CONSENT_RULES.includes(rule) ? CONSENT_RULES.indexOf(rule) : -1,
];

const atLeastAsStrict = (a, b) => a[0] > b[0] || (a[0] === b[0] && a[1] >= b[1]);

/**
 * Whether the deployment lets a meeting use `choice`, as override_permitted
 * decides it: always when it allows a per-meeting choice, and otherwise only
 * for its own mode and rule or a stricter one. `defaults` is the
 * getMeetingConsentDefaults answer ({ Mode, Rule, AllowPerMeetingOverride });
 * without it, or with a mode this form does not know, every choice is
 * offered, because the VP and the resolver still apply the rule.
 */
export const meetingConsentChoicePermitted = (choice, defaults) => {
  if (!choice || choice === 'default') return true;
  if (!defaults || defaults.AllowPerMeetingOverride !== false) return true;
  if (!CONSENT_MODES.includes(defaults.Mode)) return true;
  const { consentMode, consentRule } = meetingConsentInput(choice);
  return atLeastAsStrict(consentStrictness(consentMode, consentRule), consentStrictness(defaults.Mode, defaults.Rule));
};

/** The deployment's mode and rule in words, for the option descriptions. */
export const deploymentConsentLabel = (defaults) => {
  if (defaults?.Mode === 'off') return 'Off';
  if (defaults?.Mode === 'notify') return 'Notify';
  if (defaults?.Mode === 'opt_in') {
    return defaults.Rule === 'owner_attests'
      ? 'Opt-in, waiting until you confirm that everyone agreed'
      : 'Opt-in, waiting until every participant consents';
  }
  return '';
};

// A choice the deployment would not use is shown, but cannot be chosen.
const withDeploymentRule = (option, defaults) => {
  const label = deploymentConsentLabel(defaults);
  if (option.value === 'default') {
    return label ? { ...option, description: `This deployment uses ${label}.` } : option;
  }
  if (option.disabled || meetingConsentChoicePermitted(option.value, defaults)) return option;
  return {
    ...option,
    disabled: true,
    description: `Not allowed in this deployment: the meeting would use ${label}.`,
  };
};

// What the Virtual Participant can see on each platform decides whether the
// each-participant rule can be met there (docs/meeting-consent.md, "What each
// platform supports"). Webex shows no attendee count, so it never can. The
// Teams browser join shows no chat sender, so there consent is given aloud;
// the Teams join through Azure Communication Services reads it in the chat
// too, and which one a VP uses is decided by the deployment, so the option
// stays available for Teams with a note. On Google Meet the sender of a chat
// reply and the speaker of a spoken one are read from the page, and neither
// reading has been confirmed in a live meeting, so the option carries that
// caution.
const withPlatformRule = (option, platform) => {
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
        'On the Teams browser join, which does not show who sent a chat message, ' +
        'participants consent aloud ("LMA, I consent"); needs Read spoken replies on.',
    };
  }
  if (platform === 'GOOGLE_MEET') {
    return {
      ...option,
      description:
        'Who sent or said each reply is read from the Google Meet page and has not been ' +
        'confirmed in a live meeting. Where that matters, use "Opt-in: wait until I confirm ' +
        'that everyone agreed".',
    };
  }
  return option;
};

/**
 * The options for `platform`, each one the platform or the deployment rules
 * out shown disabled with the reason. `defaults` is as for
 * meetingConsentChoicePermitted; without it only the platform rules apply.
 */
export const meetingConsentOptionsFor = (platform, defaults = null) =>
  MEETING_CONSENT_OPTIONS.map((option) => withDeploymentRule(withPlatformRule(option, platform), defaults));

/** Whether `choice` can be offered; a choice that cannot falls back to the default. */
export const meetingConsentChoiceAvailable = (choice, platform, defaults = null) =>
  !meetingConsentOptionsFor(platform, defaults).some((option) => option.value === choice && option.disabled);
