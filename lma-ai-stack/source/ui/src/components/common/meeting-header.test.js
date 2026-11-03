/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import { describe, it, expect } from 'vitest';

import {
  formatDuration,
  formatDurationClock,
  formatMeetingStart,
  meetingDurationMillis,
  meetingTitle,
  platformLabel,
  transcriptExtentMillis,
} from './meeting-header';

describe('meetingTitle', () => {
  it.each([
    // Virtual Participant
    ['Weekly sync-2026-11-03-14-05-09-123', 'Weekly sync'],
    // Stream Audio, its embeddable form, the browser extension
    ['Weekly sync - 2026-11-03-14:05:09.123', 'Weekly sync'],
    // Desktop capture apps, with a name and without one
    ['Weekly sync - 2026-11-03 14:05', 'Weekly sync'],
    ['LMA native prototype - 2026-11-03T14:05:09Z', 'LMA native prototype'],
    // Load simulator
    ['load-00001 - 2026-11-03T14:05:09', 'load-00001'],
    // Upload Audio
    ['Weekly-sync-20261103T140509-1a2b3c4d', 'Weekly-sync'],
  ])('takes the source time stamp off %s', (callId, title) => {
    expect(meetingTitle(callId)).toBe(title);
  });

  it.each(['Weekly sync', 'Q3 review 2026-11-03', '6f1c2d9e-8a7b-4c3d-9e8f-0a1b2c3d4e5f', 'Planning - 14:05'])(
    'shows a Meeting ID with no recognised time stamp as it is: %s',
    (callId) => {
      expect(meetingTitle(callId)).toBe(callId);
    },
  );

  it('keeps a Meeting ID that is nothing but a time stamp', () => {
    expect(meetingTitle(' - 2026-11-03-14:05:09.123')).toBe(' - 2026-11-03-14:05:09.123');
  });

  it('is empty for no Meeting ID', () => {
    expect(meetingTitle(undefined)).toBe('');
  });
});

describe('formatMeetingStart', () => {
  it('shows the start in the given time zone, naming the zone', () => {
    const iso = '2026-11-03T14:05:09.123Z';
    expect(formatMeetingStart(iso, { locale: 'en-US', timeZone: 'America/Los_Angeles' })).toBe(
      'Nov 3, 2026, 6:05 AM PST',
    );
    expect(formatMeetingStart(iso, { locale: 'en-US', timeZone: 'UTC' })).toBe('Nov 3, 2026, 2:05 PM UTC');
  });

  it('is empty for a value that is not a date', () => {
    expect(formatMeetingStart('not a date')).toBe('');
    expect(formatMeetingStart(undefined)).toBe('');
  });
});

describe('durations', () => {
  it('formats minutes under an hour and hours from an hour', () => {
    expect(formatDuration(1036000)).toBe('17:16');
    expect(formatDuration(3723000)).toBe('1:02:03');
    expect(formatDuration(0)).toBe('0:00');
    expect(formatDurationClock(1036000)).toBe('00:17:16');
  });

  const segment = (channel, endTime, isPartial = false) => ({ channel, endTime, isPartial });

  it('reads how far the transcript reaches from its final speech lines only', () => {
    const segments = [
      segment('CALLER', 120),
      segment('AGENT', 1036),
      segment('CALLER', 1200, true), // partial
      segment('CHAT_ASSISTANT', 1762180000), // a wall-clock time
      segment('MEETING_ASSISTANT', 1500),
    ];
    expect(transcriptExtentMillis(segments)).toBe(1036000);
  });

  it('shows the longer of the stored length and the transcript', () => {
    const segments = [segment('CALLER', 1036)];
    // The stored figure is the end of whichever line was written last.
    expect(meetingDurationMillis({ totalConversationDurationMillis: 177000 }, segments)).toBe(1036000);
    expect(meetingDurationMillis({ totalConversationDurationMillis: 2000000 }, segments)).toBe(2000000);
    expect(meetingDurationMillis({}, [])).toBe(0);
  });
});

describe('platformLabel', () => {
  it('names the platforms a Virtual Participant joins', () => {
    expect(platformLabel('TEAMS')).toBe('Microsoft Teams');
    expect(platformLabel('ZOOM')).toBe('Zoom');
    expect(platformLabel('Something else')).toBe('Something else');
    expect(platformLabel(undefined)).toBe('');
  });
});
