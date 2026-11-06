/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import { describe, it, expect } from 'vitest';

import {
  DEPLOYMENT_DEFAULT,
  NOT_RECORDED,
  describeConsent,
  describeMeetingPassword,
  describeStart,
  describeToggle,
  describeZoomAccount,
  meetingIdLabel,
} from './launch-settings';

// 2026-11-10T19:00:00Z: 14:00 in New York, 11:00 in Los Angeles.
const MEETING_TIME = Date.UTC(2026, 10, 10, 19, 0, 0) / 1000;

describe('describeMeetingPassword', () => {
  it('says a password was set without showing it', () => {
    const text = describeMeetingPassword({ hasMeetingPassword: true, meetingPlatform: 'ZOOM', meetingId: '123' });
    expect(text).toBe('Set (not shown)');
  });

  it('says none was set', () => {
    expect(describeMeetingPassword({ hasMeetingPassword: false, meetingPlatform: 'ZOOM', meetingId: '123' })).toBe(
      'Not set',
    );
  });

  it('explains a join link and Google Meet, which never take one', () => {
    expect(
      describeMeetingPassword({ hasMeetingPassword: false, meetingPlatform: 'WEBEX', meetingId: 'https://x/j' }),
    ).toMatch(/meeting link/);
    expect(describeMeetingPassword({ hasMeetingPassword: false, meetingPlatform: 'GOOGLE_MEET' })).toMatch(
      /Google Meet/,
    );
  });

  it('does not guess when the page did not read it', () => {
    expect(describeMeetingPassword({ meetingPlatform: 'ZOOM', meetingId: '123' })).toBe(NOT_RECORDED);
  });
});

describe('meetingIdLabel', () => {
  it('uses the form labels', () => {
    expect(meetingIdLabel('ZOOM', '123 456')).toBe('Meeting ID');
    expect(meetingIdLabel('TEAMS', 'https://teams.microsoft.com/l/meetup-join/x')).toBe('Meeting URL');
    expect(meetingIdLabel('GOOGLE_MEET', 'abc-defg-hij')).toBe('Meeting code or link');
  });
});

describe('describeZoomAccount', () => {
  it('applies only to Zoom', () => {
    expect(describeZoomAccount({ meetingPlatform: 'TEAMS', useStoredZoomAccount: true })).toBeNull();
  });

  it('reads the stored choice', () => {
    expect(describeZoomAccount({ meetingPlatform: 'ZOOM', useStoredZoomAccount: true })).toMatch(/stored Zoom/);
    expect(describeZoomAccount({ meetingPlatform: 'ZOOM', useStoredZoomAccount: false })).toBe('Joined as a guest');
    expect(describeZoomAccount({ meetingPlatform: 'ZOOM' })).toBe(NOT_RECORDED);
  });
});

describe('describeStart', () => {
  it('reads an immediate launch as Start now', () => {
    expect(describeStart({ isScheduled: false }, 'UTC').primary).toBe('Start now');
  });

  it('shows the time in the zone it was entered in, and in the viewer zone when that differs', () => {
    const { primary, secondary } = describeStart(
      { isScheduled: true, meetingTime: MEETING_TIME, scheduledTimeZone: 'America/New_York' },
      'America/Los_Angeles',
    );
    expect(primary).toMatch(/2:00|14:00/);
    expect(primary).toMatch(/\(America\/New_York\)$/);
    expect(secondary).toMatch(/11:00/);
    expect(secondary).toMatch(/America\/Los_Angeles/);
  });

  it('shows one time when the viewer is in the zone it was entered in', () => {
    const { secondary } = describeStart(
      { isScheduled: true, meetingTime: MEETING_TIME, scheduledTimeZone: 'UTC' },
      'UTC',
    );
    expect(secondary).toBeNull();
  });

  it('falls back to scheduledFor and says the entry zone is unknown on older rows', () => {
    const { primary, secondary } = describeStart(
      { isScheduled: true, scheduledFor: '2026-11-10T19:00:00.000Z' },
      'UTC',
    );
    expect(primary).toMatch(/7:00|19:00/);
    expect(primary).toMatch(/\(UTC\)$/);
    expect(secondary).toMatch(/not recorded/);
  });
});

describe('describeToggle and describeConsent', () => {
  it('reads an absent setting as the deployment default', () => {
    expect(describeToggle(true)).toBe('On');
    expect(describeToggle(false)).toBe('Off');
    expect(describeToggle(null)).toBe(DEPLOYMENT_DEFAULT);
    expect(describeConsent({})).toBe(DEPLOYMENT_DEFAULT);
  });

  it('labels a consent choice as the form does', () => {
    expect(describeConsent({ consentMode: 'off' })).toBe('Off');
    expect(describeConsent({ consentMode: 'opt_in', consentRule: 'owner_attests' })).toMatch(/I confirm/);
  });
});
