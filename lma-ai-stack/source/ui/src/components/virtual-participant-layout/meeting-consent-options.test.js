/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

import { describe, expect, it } from 'vitest';
import {
  MEETING_CONSENT_OPTIONS,
  meetingConsentChoiceAvailable,
  meetingConsentChoicePermitted,
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

  it('offers every choice when the deployment settings are unknown', () => {
    expect(meetingConsentOptionsFor('ZOOM', null)).toEqual(MEETING_CONSENT_OPTIONS);
    expect(meetingConsentChoiceAvailable('off', 'ZOOM', null)).toBe(true);
  });

  it('disables a choice the deployment would not use, naming what the meeting would use', () => {
    const defaults = { Mode: 'notify', Rule: 'each_participant', AllowPerMeetingOverride: false };
    const options = meetingConsentOptionsFor('ZOOM', defaults);
    const off = options.find((o) => o.value === 'off');
    expect(off.disabled).toBe(true);
    expect(off.description).toBe('Not allowed in this deployment: the meeting would use Notify.');
    expect(meetingConsentChoiceAvailable('off', 'ZOOM', defaults)).toBe(false);
    const byDefault = options.find((o) => o.value === 'default');
    expect(byDefault.disabled).toBeUndefined();
    expect(byDefault.description).toBe('This deployment uses Notify.');
    ['notify', 'opt_in:owner_attests', 'opt_in:each_participant'].forEach((value) => {
      expect(options.find((o) => o.value === value).disabled).toBeUndefined();
    });
  });

  it('keeps the platform reason for a choice both the platform and the deployment rule out', () => {
    const defaults = { Mode: 'opt_in', Rule: 'each_participant', AllowPerMeetingOverride: false };
    const options = meetingConsentOptionsFor('WEBEX', defaults);
    const each = options.find((o) => o.value === 'opt_in:each_participant');
    expect(each.disabled).toBe(true);
    expect(each.description).toMatch(/Not available on Webex/);
    const owner = options.find((o) => o.value === 'opt_in:owner_attests');
    expect(owner.disabled).toBe(true);
    expect(owner.description).toBe(
      'Not allowed in this deployment: the meeting would use Opt-in, waiting until every participant consents.',
    );
    expect(options.find((o) => o.value === 'default').disabled).toBeUndefined();
  });

  it('keeps the Teams note on an each-participant choice the deployment allows', () => {
    const defaults = { Mode: 'opt_in', Rule: 'owner_attests', AllowPerMeetingOverride: false };
    const each = meetingConsentOptionsFor('TEAMS', defaults).find((o) => o.value === 'opt_in:each_participant');
    expect(each.disabled).toBeUndefined();
    expect(each.description).toMatch(/consent aloud/);
  });

  it('offers the each-participant rule on Google Meet, with a note on how replies are attributed', () => {
    const meet = meetingConsentOptionsFor('GOOGLE_MEET').find((o) => o.value === 'opt_in:each_participant');
    expect(meet.disabled).toBeUndefined();
    expect(meet.description).toMatch(/not been confirmed in a live meeting/);
    expect(meetingConsentChoiceAvailable('opt_in:each_participant', 'GOOGLE_MEET')).toBe(true);
    const others = meetingConsentOptionsFor('GOOGLE_MEET').filter((o) => o.value !== 'opt_in:each_participant');
    expect(others).toEqual(MEETING_CONSENT_OPTIONS.filter((o) => o.value !== 'opt_in:each_participant'));
  });
});

// What lma_consent.rules.override_permitted answers for each deployment mode
// and rule with Allow a different choice per meeting off, printed from it on
// 2026-11-13. With the setting on, every choice is permitted.
const PERMITTED_WITHOUT_OVERRIDE = {
  'off:owner_attests': ['off', 'notify', 'opt_in:owner_attests', 'opt_in:each_participant'],
  'off:each_participant': ['off', 'notify', 'opt_in:owner_attests', 'opt_in:each_participant'],
  'notify:owner_attests': ['notify', 'opt_in:owner_attests', 'opt_in:each_participant'],
  'notify:each_participant': ['notify', 'opt_in:owner_attests', 'opt_in:each_participant'],
  'opt_in:owner_attests': ['opt_in:owner_attests', 'opt_in:each_participant'],
  'opt_in:each_participant': ['opt_in:each_participant'],
};
const CHOICES = ['off', 'notify', 'opt_in:owner_attests', 'opt_in:each_participant'];

describe('meetingConsentChoicePermitted', () => {
  MODES.forEach((Mode) => {
    RULES.forEach((Rule) => {
      [true, false].forEach((AllowPerMeetingOverride) => {
        it(`matches the backend for ${Mode} (${Rule}) with per-meeting choice ${AllowPerMeetingOverride}`, () => {
          const defaults = { Mode, Rule, AllowPerMeetingOverride };
          const expected = AllowPerMeetingOverride ? CHOICES : PERMITTED_WITHOUT_OVERRIDE[`${Mode}:${Rule}`];
          expect(CHOICES.filter((choice) => meetingConsentChoicePermitted(choice, defaults))).toEqual(expected);
          expect(meetingConsentChoicePermitted('default', defaults)).toBe(true);
        });
      });
    });
  });

  it('permits every choice when the settings are unknown or incomplete', () => {
    [null, undefined, {}, { Mode: 'notify' }, { Mode: 'unknown', AllowPerMeetingOverride: false }].forEach(
      (defaults) => {
        CHOICES.forEach((choice) => expect(meetingConsentChoicePermitted(choice, defaults)).toBe(true));
      },
    );
  });
});
