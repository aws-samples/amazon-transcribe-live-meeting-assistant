/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import { describe, it, expect } from 'vitest';

import {
  describeGuidance,
  guidanceChoiceOf,
  guidanceCreateInput,
  guidanceEditInput,
  GUIDANCE_OPTIONS,
} from './guidance-options';
import { editFormFrom, scheduledEditInput } from './scheduled-edit';
import { joinAgainFormFrom, joinAgainInputFrom } from './join-again';

describe('the live guidance choice', () => {
  it('offers the deployment setting first, then on and off', () => {
    expect(GUIDANCE_OPTIONS.map((option) => option.value)).toEqual(['default', 'on', 'off']);
  });

  it('sends nothing on create for the deployment setting, and true or false otherwise', () => {
    expect(guidanceCreateInput('default')).toEqual({});
    expect(guidanceCreateInput('on')).toEqual({ guidanceEnabled: true });
    expect(guidanceCreateInput('off')).toEqual({ guidanceEnabled: false });
  });

  it('reads a stored row', () => {
    expect(guidanceChoiceOf({ guidanceEnabled: true })).toBe('on');
    expect(guidanceChoiceOf({ guidanceEnabled: false })).toBe('off');
    expect(guidanceChoiceOf({})).toBe('default');
    expect(guidanceChoiceOf(null)).toBe('default');
    expect(describeGuidance({ guidanceEnabled: false })).toBe('Off');
  });

  it('sends a change on edit, null to go back to the deployment setting, and nothing unchanged', () => {
    expect(guidanceEditInput('on', {})).toEqual({ guidanceEnabled: true });
    expect(guidanceEditInput('default', { guidanceEnabled: true })).toEqual({ guidanceEnabled: null });
    expect(guidanceEditInput('off', { guidanceEnabled: false })).toEqual({});
  });

  it('is part of a scheduled edit', () => {
    const vp = {
      id: 'vp1',
      meetingName: 'Sync',
      meetingPlatform: 'TEAMS',
      meetingId: 'https://teams.example.com/l/meetup-join/abc',
      meetingTime: Math.floor(Date.now() / 1000) + 3600,
      guidanceEnabled: true,
    };
    const values = editFormFrom(vp);
    expect(values.guidanceChoice).toBe('on');
    expect(scheduledEditInput(vp, values, { zoomChoiceOffered: false })).toBeNull();
    expect(scheduledEditInput(vp, { ...values, guidanceChoice: 'default' }, { zoomChoiceOffered: false })).toEqual({
      id: 'vp1',
      guidanceEnabled: null,
    });
  });

  it('is carried by Join again, false included', () => {
    expect(joinAgainFormFrom({ id: 'vp1', guidanceEnabled: false }).guidanceChoice).toBe('off');
    const input = joinAgainInputFrom('vp1', { meetingName: 'Sync', guidanceEnabled: false });
    expect(input.guidanceEnabled).toBe(false);
    expect('guidanceEnabled' in joinAgainInputFrom('vp1', { meetingName: 'Sync' })).toBe(false);
  });
});
