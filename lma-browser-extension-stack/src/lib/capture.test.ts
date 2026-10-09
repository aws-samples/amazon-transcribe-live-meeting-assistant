/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The browser extension's first tests.
 *
 * It had none, and no CI job, so nothing checked it at all. These cover the three
 * pieces of real logic behind audio capture, extracted from
 * `ProviderIntegrationContext` in `src/lib/capture.ts` so they can be called
 * without rendering a component that opens a WebSocket.
 *
 * The mute tests carry the most weight. `applyMuteAndPause` is what decides
 * whether a user who pressed mute is actually silent: if the channel offset or
 * the stride is wrong, live microphone audio is streamed from someone who
 * believes they are muted. So the assertions check both that the microphone
 * channel IS silenced and that the meeting channel is NOT — a test that only
 * checked the first would pass against code that silenced everything.
 */

import {
  applyMuteAndPause,
  formatTimestamp,
  mergeMetadata,
  mergeMetadataForTab,
  platformFromBaseUrl,
  prefillField,
  UNKNOWN_PLATFORM,
} from './capture';

/**
 * Two frames of stereo 16-bit audio with every byte non-zero, so that any byte
 * left untouched is visibly distinct from a byte that was zeroed.
 * Layout per 4-byte frame: [meeting-lo, meeting-hi, mic-lo, mic-hi].
 */
function twoFrames(): Uint8Array {
  return new Uint8Array([11, 12, 13, 14, 21, 22, 23, 24]);
}

describe('applyMuteAndPause', () => {
  it('passes audio through untouched when neither muted nor paused', () => {
    expect(Array.from(applyMuteAndPause(twoFrames(), false, false))).toEqual([
      11, 12, 13, 14, 21, 22, 23, 24,
    ]);
  });

  it('silences the microphone channel when muted', () => {
    expect(Array.from(applyMuteAndPause(twoFrames(), true, false))).toEqual([
      11, 12, 0, 0, 21, 22, 0, 0,
    ]);
  });

  it('leaves the meeting channel audible when muted', () => {
    // The point of mute rather than pause: the meeting is still recorded.
    const result = applyMuteAndPause(twoFrames(), true, false);
    expect(result[0]).toBe(11);
    expect(result[1]).toBe(12);
    expect(result[4]).toBe(21);
    expect(result[5]).toBe(22);
  });

  it('silences every channel when paused', () => {
    expect(Array.from(applyMuteAndPause(twoFrames(), true, true))).toEqual([
      0, 0, 0, 0, 0, 0, 0, 0,
    ]);
  });

  it('silences every channel when paused even if not muted', () => {
    expect(Array.from(applyMuteAndPause(twoFrames(), false, true))).toEqual([
      0, 0, 0, 0, 0, 0, 0, 0,
    ]);
  });

  it('keeps the chunk the same length when paused', () => {
    // A shorter chunk would shift the stream's timing rather than silence it.
    const input = twoFrames();
    expect(applyMuteAndPause(input, false, true).length).toBe(input.length);
  });

  it('silences the microphone channel in every frame, not just the first', () => {
    // Catches a stride that advances by the wrong number of bytes.
    const tenFrames = new Uint8Array(40).fill(9);
    const result = applyMuteAndPause(tenFrames, true, false);
    for (let frame = 0; frame < 10; frame += 1) {
      expect(result[frame * 4 + 2]).toBe(0);
      expect(result[frame * 4 + 3]).toBe(0);
      expect(result[frame * 4]).toBe(9);
      expect(result[frame * 4 + 1]).toBe(9);
    }
  });

  it('returns an all-zero array for an empty chunk rather than throwing', () => {
    expect(applyMuteAndPause(new Uint8Array(0), true, true).length).toBe(0);
    expect(applyMuteAndPause(new Uint8Array(0), true, false).length).toBe(0);
  });

  it('tolerates a chunk that is not a whole number of frames', () => {
    // Writing past the end of a Uint8Array is silently ignored, so this must not
    // throw; the bytes that do exist are still handled.
    const partial = new Uint8Array([1, 2, 3]);
    expect(() => applyMuteAndPause(partial, true, false)).not.toThrow();
    expect(partial[2]).toBe(0);
  });

  it('modifies the caller’s array in place when muting', () => {
    // Documented because callers rely on it, and because it is the asymmetry
    // with pause: pause hands back a fresh array instead.
    const input = twoFrames();
    const result = applyMuteAndPause(input, true, false);
    expect(result).toBe(input);
    expect(input[2]).toBe(0);
  });

  it('leaves the caller’s array alone when pausing', () => {
    const input = twoFrames();
    const result = applyMuteAndPause(input, false, true);
    expect(result).not.toBe(input);
    expect(input[0]).toBe(11);
  });
});

describe('platformFromBaseUrl', () => {
  it.each([
    ['https://app.zoom.us', 'Zoom'],
    ['https://app.chime.aws', 'Amazon Chime'],
    ['https://meet.google.com', 'Google Meet'],
  ])('names the platform for %s', (baseUrl, expected) => {
    expect(platformFromBaseUrl(baseUrl)).toBe(expected);
  });

  it.each([
    'https://teams.microsoft.com',
    'https://teams.live.com',
    'https://teams.microsoft.us',
    'https://teams.cloud.microsoft',
    'https://teams.microsoft.com/v2/?meetingjoin=true',
    'https://teams.cloud.microsoft/v2/',
  ])('recognises Teams at %s', (baseUrl) => {
    // Teams serves meetings from four hostnames; missing one means the UI shows
    // no platform for a meeting it is nonetheless capturing.
    expect(platformFromBaseUrl(baseUrl)).toBe('Microsoft Teams');
  });

  it.each([
    ['https://us02web.zoom.us', 'Zoom'],
    ['https://zoom.us', 'Zoom'],
    ['https://acme.teams.cloud.microsoft', 'Microsoft Teams'],
  ])('recognises %s by subdomain as %s', (baseUrl, expected) => {
    // Zoom routes the web client through numbered regional hosts, and the
    // manifest registers the reader for all of `*.zoom.us`, so the platform
    // label has to cover the same range the reader does.
    expect(platformFromBaseUrl(baseUrl)).toBe(expected);
  });

  it('names the platform from a bare hostname with no scheme', () => {
    // Every reader but Zoom reports `window.location.origin`; Zoom's value comes
    // out of Zoom's own MeetingConfig object, whose format is not ours to fix.
    expect(platformFromBaseUrl('app.zoom.us')).toBe('Zoom');
  });

  it.each([
    'https://webex.com',
    'https://acme.webex.com',
    'https://meet1234.webex.com',
  ])('recognises Webex at %s', (baseUrl) => {
    // Webex is per-organisation, so it has to match by substring.
    expect(platformFromBaseUrl(baseUrl)).toBe('Cisco Webex');
  });

  it.each<[string, string | undefined | null]>([
    ['an empty string', ''],
    ['undefined', undefined],
    ['null', null],
  ])('reports an unknown platform for %s', (_label, baseUrl) => {
    expect(platformFromBaseUrl(baseUrl)).toBe(UNKNOWN_PLATFORM);
  });

  it('reports an unknown platform for an unrelated site', () => {
    // Guessed attribution would label an ordinary tab as a meeting.
    expect(platformFromBaseUrl('https://example.com')).toBe(UNKNOWN_PLATFORM);
  });

  it('does not mistake a lookalike host for Zoom or Chime', () => {
    // Matching is on the parsed hostname, so a host that merely ends with or
    // contains a platform's name is not attributed to it.
    expect(platformFromBaseUrl('https://app.zoom.us.example.com')).toBe(UNKNOWN_PLATFORM);
    expect(platformFromBaseUrl('https://notapp.chime.aws')).toBe(UNKNOWN_PLATFORM);
    expect(platformFromBaseUrl('https://teams.microsoft.com.example.com')).toBe(UNKNOWN_PLATFORM);
    expect(platformFromBaseUrl('https://notzoom.us')).toBe(UNKNOWN_PLATFORM);
    expect(platformFromBaseUrl('https://example.com/?next=https://teams.cloud.microsoft'))
      .toBe(UNKNOWN_PLATFORM);
  });
});

describe('mergeMetadata', () => {
  it('keeps fields the update does not mention', () => {
    // The readers report whatever they have found so far, and with `all_frames`
    // enabled several frames of one tab report independently — so an update
    // carrying only `baseUrl` can arrive after one that carried the name.
    const merged = mergeMetadata(
      { userName: 'Ada Lovelace', meetingTopic: 'Weekly', baseUrl: 'https://teams.cloud.microsoft' },
      { baseUrl: 'https://teams.cloud.microsoft' },
    );
    expect(merged.userName).toBe('Ada Lovelace');
    expect(merged.meetingTopic).toBe('Weekly');
  });

  it('does not let a blank field overwrite a known one', () => {
    const merged = mergeMetadata(
      { userName: 'Ada Lovelace' },
      { userName: '   ', meetingTopic: 'Weekly' },
    );
    expect(merged.userName).toBe('Ada Lovelace');
    expect(merged.meetingTopic).toBe('Weekly');
  });

  it('applies non-blank fields from the update', () => {
    const merged = mergeMetadata({ userName: 'Stale Name' }, { userName: 'Ada Lovelace' });
    expect(merged.userName).toBe('Ada Lovelace');
  });

  it('treats a missing side as empty rather than throwing', () => {
    expect(mergeMetadata(undefined, { userName: 'Ada Lovelace' })).toEqual({ userName: 'Ada Lovelace' });
    expect(mergeMetadata({ userName: 'Ada Lovelace' }, null)).toEqual({ userName: 'Ada Lovelace' });
    expect(mergeMetadata(null, undefined)).toEqual({});
  });

  it('does not mutate either argument', () => {
    const previous = { userName: 'Ada Lovelace' };
    const incoming = { meetingTopic: 'Weekly' };
    mergeMetadata(previous, incoming);
    expect(previous).toEqual({ userName: 'Ada Lovelace' });
    expect(incoming).toEqual({ meetingTopic: 'Weekly' });
  });

  it('keeps a false or zero value, which are not blank', () => {
    // Only undefined, null and whitespace-only strings count as "nothing found".
    const merged = mergeMetadata({ muted: true }, { muted: false, samplingRate: 0 });
    expect(merged.muted).toBe(false);
    expect(merged.samplingRate).toBe(0);
  });
});

describe('mergeMetadataForTab', () => {
  const onTabOne = {
    tabId: 1,
    metadata: { userName: 'Ada Lovelace', meetingTopic: 'Monday standup' },
  };

  it('merges an update from the same tab', () => {
    const known = mergeMetadataForTab(onTabOne, { baseUrl: 'https://teams.cloud.microsoft' }, 1);
    expect(known.metadata.meetingTopic).toBe('Monday standup');
    expect(known.metadata.baseUrl).toBe('https://teams.cloud.microsoft');
    expect(known.tabId).toBe(1);
  });

  it('does not carry a topic from one tab into another', () => {
    // The topic becomes the meeting's name in LMA. A meeting whose own reader
    // found no topic must not be recorded under the previous meeting's name.
    const known = mergeMetadataForTab(onTabOne, { baseUrl: 'https://app.zoom.us' }, 2);
    expect(known.metadata.meetingTopic).toBeUndefined();
    expect(known.metadata.userName).toBeUndefined();
    expect(known.metadata.baseUrl).toBe('https://app.zoom.us');
    expect(known.tabId).toBe(2);
  });

  it('treats an update with no tab as belonging to the tab in hand', () => {
    // Nothing better to assume, and discarding would blank fields the panel has
    // already shown.
    const known = mergeMetadataForTab(onTabOne, { baseUrl: 'https://teams.cloud.microsoft' });
    expect(known.metadata.meetingTopic).toBe('Monday standup');
    expect(known.tabId).toBe(1);
  });

  it('adopts the first tab it hears from', () => {
    const known = mergeMetadataForTab({ metadata: {} }, { userName: 'Ada Lovelace' }, 7);
    expect(known.tabId).toBe(7);
    expect(known.metadata.userName).toBe('Ada Lovelace');
  });

  it('does not mutate what it was given', () => {
    mergeMetadataForTab(onTabOne, { meetingTopic: 'Tuesday review' }, 2);
    expect(onTabOne.metadata.meetingTopic).toBe('Monday standup');
    expect(onTabOne.tabId).toBe(1);
  });
});

describe('prefillField', () => {
  it('fills a field the user has not filled', () => {
    expect(prefillField('', '', 'Monday standup')).toBe('Monday standup');
  });

  it('leaves a value the user typed alone', () => {
    // The readers report on page load, on every request from the panel, and
    // again when a late source such as Zoom's MeetingConfig turns up — so this
    // is what stops a report landing two seconds after the user started typing
    // from eating what they typed.
    expect(prefillField('Budget review', '', 'Monday standup')).toBe('Budget review');
  });

  it('refreshes a field that still holds our own prefill', () => {
    // How switching the panel to a different meeting tab updates the field.
    expect(prefillField('Monday standup', 'Monday standup', 'Tuesday review'))
      .toBe('Tuesday review');
  });

  it('leaves a field the user emptied empty', () => {
    // Backspacing to nothing is how someone starts retyping a topic. Refilling
    // it under them would make the field impossible to clear.
    expect(prefillField('', 'Monday standup', 'Tuesday review')).toBe('');
  });

  it('keeps the current value when nothing was reported', () => {
    expect(prefillField('Budget review', '', undefined)).toBe('Budget review');
    expect(prefillField('Budget review', '', null)).toBe('Budget review');
    expect(prefillField('Budget review', '', '')).toBe('Budget review');
    expect(prefillField('Budget review', '', '   ')).toBe('Budget review');
  });

  it('distinguishes a never-filled field from one the user emptied', () => {
    // Both are empty; what separates them is whether anything was prefilled.
    expect(prefillField('', '', 'Monday standup')).toBe('Monday standup');
    expect(prefillField('', 'Monday standup', 'Monday standup')).toBe('');
  });

  it('does not blank a prefilled field when nothing is reported', () => {
    // A thinner later report must not clear what an earlier one supplied.
    expect(prefillField('Monday standup', 'Monday standup', undefined)).toBe('Monday standup');
  });

  it('ignores a non-string report rather than stringifying it', () => {
    // Zoom's MeetingConfig is Zoom's shape, not ours; a number would render as
    // a meeting name.
    expect(prefillField('', '', 1234567890)).toBe('');
    expect(prefillField('', '', { topic: 'Monday standup' })).toBe('');
  });

  it('trims the reported value', () => {
    expect(prefillField('', '', '  Monday standup  ')).toBe('Monday standup');
  });
});

describe('formatTimestamp', () => {
  it('formats a date with every field zero-padded', () => {
    // Month is 1-based in the output but 0-based in the Date constructor.
    expect(formatTimestamp(new Date(2026, 8, 7, 6, 5, 4, 3))).toBe('2026-09-07-06:05:04.003');
  });

  it('pads milliseconds to three digits', () => {
    // Two digits would sort 999ms before 99ms.
    expect(formatTimestamp(new Date(2026, 0, 1, 0, 0, 0, 7))).toBe('2026-01-01-00:00:00.007');
  });

  it('does not pad a four-digit year or truncate it', () => {
    expect(formatTimestamp(new Date(2026, 11, 31, 23, 59, 59, 999))).toBe(
      '2026-12-31-23:59:59.999',
    );
  });

  it('produces strings that sort chronologically', () => {
    // The meeting list orders call ids as plain strings, so lexical order has to
    // match chronological order.
    const earlier = formatTimestamp(new Date(2026, 8, 7, 9, 0, 0, 0));
    const later = formatTimestamp(new Date(2026, 8, 7, 10, 0, 0, 0));
    expect(earlier < later).toBe(true);
  });

  it('sorts across a month boundary', () => {
    const endOfSeptember = formatTimestamp(new Date(2026, 8, 30, 23, 59, 59, 999));
    const startOfOctober = formatTimestamp(new Date(2026, 9, 1, 0, 0, 0, 0));
    expect(endOfSeptember < startOfOctober).toBe(true);
  });
});
