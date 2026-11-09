/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

import { describe, expect, it } from 'vitest';
import {
  MEETING_CONSENT_OPTIONS,
  meetingConsentChoiceAvailable,
  meetingConsentInput,
  meetingConsentOptionsFor,
} from './meeting-consent-options';

// The values createVirtualParticipant's resolver accepts (lma-ai-stack.yaml).
const MODES = ['off', 'notify', 'opt_in'];
const RULES = ['each_participant', 'owner_attests'];

describe('meetingConsentInput', () => {
  it("sends nothing for the deployment's setting", () => {
    expect(meetingConsentInput('default')).toEqual({});
    expect(meetingConsentInput(undefined)).toEqual({});
  });

  it('splits an opt-in choice into its mode and rule', () => {
    expect(meetingConsentInput('opt_in:owner_attests')).toEqual({
      consentMode: 'opt_in',
      consentRule: 'owner_attests',
    });
    expect(meetingConsentInput('notify')).toEqual({ consentMode: 'notify' });
  });

  it('offers only values the resolver accepts', () => {
    MEETING_CONSENT_OPTIONS.filter((o) => o.value !== 'default').forEach((option) => {
      const input = meetingConsentInput(option.value);
      expect(MODES).toContain(input.consentMode);
      if (input.consentRule !== undefined) {
        expect(input.consentMode).toBe('opt_in');
        expect(RULES).toContain(input.consentRule);
      }
    });
  });
});

describe('meetingConsentOptionsFor', () => {
  it('does not offer the each-participant rule on Webex, and notes how Teams browser participants consent', () => {
    const webex = meetingConsentOptionsFor('WEBEX').find((o) => o.value === 'opt_in:each_participant');
    expect(webex.disabled).toBe(true);
    expect(meetingConsentChoiceAvailable('opt_in:each_participant', 'WEBEX')).toBe(false);
    expect(meetingConsentChoiceAvailable('opt_in:owner_attests', 'WEBEX')).toBe(true);
    const teams = meetingConsentOptionsFor('TEAMS').find((o) => o.value === 'opt_in:each_participant');
    expect(teams.disabled).toBeUndefined();
    expect(teams.description).toMatch(/consent aloud/);
    expect(meetingConsentOptionsFor('ZOOM')).toEqual(MEETING_CONSENT_OPTIONS);
  });

  it('offers the each-participant rule on Google Meet', () => {
    expect(meetingConsentOptionsFor('GOOGLE_MEET')).toEqual(MEETING_CONSENT_OPTIONS);
    expect(meetingConsentChoiceAvailable('opt_in:each_participant', 'GOOGLE_MEET')).toBe(true);
  });
});
