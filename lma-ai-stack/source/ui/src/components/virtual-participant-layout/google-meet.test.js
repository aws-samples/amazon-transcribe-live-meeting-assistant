/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';
import { googleMeetCode, googleMeetIdError } from './google-meet';

// The table of Meet inputs every implementation of the Meet code rule runs. The
// test environment is jsdom, whose import.meta.url is not a file URL, so the
// path is built from __dirname, which vitest provides.
const SHARED_MEET_CASES = JSON.parse(
  readFileSync(resolve(__dirname, '../../../../../../test-fixtures/google-meet-inputs.json'), 'utf8'),
).cases;

describe('the shared Google Meet input table', () => {
  it('was read', () => {
    expect(SHARED_MEET_CASES.length).toBeGreaterThan(20);
  });

  it.each(SHARED_MEET_CASES)('reduces $input to $code', ({ input, code }) => {
    expect(googleMeetCode(input)).toBe(code);
  });
});

describe('googleMeetCode', () => {
  it('accepts a code in any case, with or without hyphens or spaces', () => {
    ['abc-defg-hij', 'ABC-DEFG-HIJ', 'abcdefghij', ' abc defg hij '].forEach((value) => {
      expect(googleMeetCode(value)).toBe('abc-defg-hij');
    });
  });

  it('reduces a meet.google.com link to its code, including the /meet/ form Meet redirects to', () => {
    [
      'https://meet.google.com/abc-defg-hij',
      'https://meet.google.com/abc-defg-hij?authuser=0',
      'meet.google.com/abc-defg-hij',
      'https://meet.google.com/meet/abc-defg-hij',
      'http://MEET.GOOGLE.COM/ABCDEFGHIJ/',
    ].forEach((value) => {
      expect(googleMeetCode(value)).toBe('abc-defg-hij');
    });
  });

  it('refuses anything that is not a Meet code or link', () => {
    [
      '',
      null,
      undefined,
      '96187501703',
      'abc-defg-hi',
      'https://zoom.us/j/96187501703',
      'https://meet.google.com.example.org/abc-defg-hij',
      'https://example.org/?u=meet.google.com/abc-defg-hij',
    ].forEach((value) => {
      expect(googleMeetCode(value)).toBeNull();
    });
  });
});

describe('googleMeetIdError', () => {
  it('says nothing until something is typed, and nothing for a valid code', () => {
    expect(googleMeetIdError('')).toBe('');
    expect(googleMeetIdError('   ')).toBe('');
    expect(googleMeetIdError('abc-defg-hij')).toBe('');
  });

  it('explains what to enter for anything else', () => {
    expect(googleMeetIdError('12345')).toMatch(/meeting code/);
  });

  it('says why a meeting nickname link is refused', () => {
    expect(googleMeetIdError('https://meet.google.com/lookup/team-standup')).toMatch(/nickname/);
  });
});
