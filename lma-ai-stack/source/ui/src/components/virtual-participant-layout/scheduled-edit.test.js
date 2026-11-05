/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import { describe, it, expect } from 'vitest';

import { canEditScheduled, editFormFrom, editWindowOpen, scheduledEditInput } from './scheduled-edit';
import { formDateAndTime, meetingTimeError } from './vp-form';

const HOUR_AHEAD = Math.floor(Date.now() / 1000 / 60) * 60 + 3600;

const SCHEDULED = {
  id: 'vp-1',
  Owner: 'owner@example.com',
  status: 'SCHEDULED',
  isScheduled: true,
  meetingName: 'Weekly sync',
  meetingPlatform: 'ZOOM',
  meetingId: '1234567890',
  hasMeetingPassword: true,
  meetingTime: HOUR_AHEAD,
  agenda: 'Intro\nBudget',
  consentMode: 'notify',
  enableVideoRecording: true,
  useStoredZoomAccount: true,
};

const unchanged = () => editFormFrom(SCHEDULED);
const withForm = (patch) => {
  const state = unchanged();
  return { ...state, form: { ...state.form, ...patch } };
};
const OFFERED = { zoomChoiceOffered: true };

describe('canEditScheduled', () => {
  it('is offered to the owner and Admins while the VP waits for its launch', () => {
    expect(canEditScheduled(SCHEDULED, { username: 'owner@example.com', isAdmin: false })).toBe(true);
    expect(canEditScheduled(SCHEDULED, { username: 'admin@example.com', isAdmin: true })).toBe(true);
  });

  it('is not offered to anyone else, including a user it is shared with', () => {
    const shared = { ...SCHEDULED, SharedWith: 'bob@example.com' };
    expect(canEditScheduled(shared, { username: 'bob@example.com', isAdmin: false })).toBe(false);
    expect(canEditScheduled(SCHEDULED, { username: undefined, isAdmin: false })).toBe(false);
  });

  it('is not offered once the VP has started, ended or was not scheduled', () => {
    const owner = { username: 'owner@example.com', isAdmin: false };
    expect(canEditScheduled({ ...SCHEDULED, status: 'INITIALIZING' }, owner)).toBe(false);
    expect(canEditScheduled({ ...SCHEDULED, status: 'CANCELLED' }, owner)).toBe(false);
    expect(canEditScheduled({ ...SCHEDULED, isScheduled: false }, owner)).toBe(false);
  });
});

describe('editWindowOpen', () => {
  it('closes five minutes before the meeting', () => {
    const now = HOUR_AHEAD * 1000;
    expect(editWindowOpen(SCHEDULED, now - 6 * 60 * 1000)).toBe(true);
    expect(editWindowOpen(SCHEDULED, now - 4 * 60 * 1000)).toBe(false);
  });
});

describe('editFormFrom', () => {
  it('pre-fills the stored values, with an empty password', () => {
    const { form, consentChoice, enableVideoRecording, useZoomLogin } = unchanged();
    expect(form.meetingName).toBe('Weekly sync');
    expect(form.meetingPassword).toBe('');
    expect(form).toMatchObject(formDateAndTime(HOUR_AHEAD));
    expect(consentChoice).toBe('notify');
    expect(enableVideoRecording).toBe(true);
    expect(useZoomLogin).toBe(true);
  });
});

describe('scheduledEditInput', () => {
  it('sends nothing when nothing changed', () => {
    expect(scheduledEditInput(SCHEDULED, unchanged(), OFFERED)).toBeNull();
  });

  it('sends only what changed', () => {
    expect(scheduledEditInput(SCHEDULED, withForm({ meetingName: 'Renamed' }), OFFERED)).toEqual({
      id: 'vp-1',
      meetingName: 'Renamed',
    });
  });

  it('keeps the current password when the field is left empty, and sends a new one', () => {
    expect(scheduledEditInput(SCHEDULED, withForm({ meetingPassword: '' }), OFFERED)).toBeNull();
    expect(scheduledEditInput(SCHEDULED, withForm({ meetingPassword: 'new' }), OFFERED)).toEqual({
      id: 'vp-1',
      meetingPassword: 'new',
    });
  });

  it('removes the password when the meeting becomes a join link', () => {
    const input = scheduledEditInput(SCHEDULED, withForm({ meetingId: 'https://zoom.us/j/1' }), OFFERED);
    expect(input).toMatchObject({ meetingId: 'https://zoom.us/j/1', clearMeetingPassword: true });
    expect(input.meetingPassword).toBeUndefined();
  });

  it('sends a new time with the time zone it was entered in', () => {
    const later = formDateAndTime(HOUR_AHEAD + 1800);
    const input = scheduledEditInput(SCHEDULED, withForm(later), OFFERED);
    expect(input.meetingTime).toBe(HOUR_AHEAD + 1800);
    expect(typeof input.scheduledTimeZone).toBe('string');
  });

  it('clears the agenda and the consent choice with empty values', () => {
    const state = { ...withForm({ agenda: '   ' }), consentChoice: 'default' };
    expect(scheduledEditInput(SCHEDULED, state, OFFERED)).toEqual({ id: 'vp-1', agenda: '', consentMode: '' });
  });

  it('sends an opt-in rule with its mode', () => {
    const state = { ...unchanged(), consentChoice: 'opt_in:owner_attests' };
    expect(scheduledEditInput(SCHEDULED, state, OFFERED)).toMatchObject({
      consentMode: 'opt_in',
      consentRule: 'owner_attests',
    });
  });

  it('joins as a guest when the meeting is no longer on Zoom', () => {
    const input = scheduledEditInput(SCHEDULED, withForm({ meetingPlatform: 'TEAMS' }), OFFERED);
    expect(input).toMatchObject({ meetingPlatform: 'TEAMS', useStoredZoomAccount: false });
  });

  it('sends the video recording and Zoom choices when they change', () => {
    const state = { ...unchanged(), enableVideoRecording: false, useZoomLogin: false };
    expect(scheduledEditInput(SCHEDULED, state, OFFERED)).toEqual({
      id: 'vp-1',
      enableVideoRecording: false,
      useStoredZoomAccount: false,
    });
  });
});

describe('meetingTimeError', () => {
  it('holds an edit to five minutes ahead and a new VP to two', () => {
    const now = new Date('2026-11-10T12:00:00');
    expect(meetingTimeError('2026-11-10', '12:04', 5, now)).toMatch(/at least 5 minutes/);
    expect(meetingTimeError('2026-11-10', '12:05', 5, now)).toBe('');
    expect(meetingTimeError('2026-11-10', '12:01', 2, now)).toMatch(/at least two minutes/);
    expect(meetingTimeError('2026-11-10', '12:0', 2, now)).toMatch(/incomplete/);
    expect(meetingTimeError('', '12:30', 2, now)).toMatch(/date first/);
  });
});
