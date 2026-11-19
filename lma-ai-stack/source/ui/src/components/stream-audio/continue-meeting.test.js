/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import { describe, expect, it, vi } from 'vitest';

import { INVALID_MEETING_ID, describeReopenFailure, meetingIdFrom, reopenMeeting } from './continue-meeting';

const CALL_ID = 'Weekly sync - 2026-11-19-10:00:00.000';

describe('the meeting ID in what was pasted', () => {
  it.each([
    [CALL_ID, CALL_ID],
    [`  ${CALL_ID}\n`, CALL_ID],
    ['https://d1.cloudfront.net/#/calls/Weekly%20sync%20-%202026', 'Weekly sync - 2026'],
    ['https://d1.cloudfront.net/#/calls/m%2F1/?tab=summary', 'm/1'],
    ['https://d1.cloudfront.net/#/calls/bad%E0%A4', 'bad%E0%A4'],
    ['https://example.com/other', null],
    ['https://d1.cloudfront.net/#/calls/', null],
    ['', null],
    [undefined, null],
  ])('%j is %j', (input, expected) => {
    expect(meetingIdFrom(input)).toBe(expected);
  });
});

describe('reopening a meeting', () => {
  const client = (answer) => ({ graphql: vi.fn(answer) });

  it('answers the session the server allowed', async () => {
    const api = client(async () => ({
      data: { reopenMeeting: { CallId: CALL_ID, SessionNumber: 2, SessionOffsetSeconds: 1807 } },
    }));
    expect(await reopenMeeting(api, `https://x/#/calls/${encodeURIComponent(CALL_ID)}`)).toEqual({
      session: { callId: CALL_ID, sessionNumber: 2, offsetSeconds: 1807 },
    });
    expect(api.graphql.mock.calls[0][0].variables).toEqual({ input: { CallId: CALL_ID } });
  });

  it('asks nothing of the server for nothing usable', async () => {
    const api = client(async () => ({}));
    expect(await reopenMeeting(api, 'https://example.com/x')).toEqual({ error: INVALID_MEETING_ID });
    expect(api.graphql).not.toHaveBeenCalled();
  });

  it("shows the server's refusal", async () => {
    const refusal = { errors: [{ errorType: 'MeetingInProgress', message: 'Still being captured, from the server.' }] };
    const api = client(async () => {
      throw refusal;
    });
    expect(await reopenMeeting(api, CALL_ID)).toEqual({ error: 'Still being captured, from the server.' });
  });

  it('takes an answer that names no later session as a failure', async () => {
    const api = client(async () => ({ data: { reopenMeeting: { CallId: CALL_ID, SessionNumber: 1 } } }));
    expect((await reopenMeeting(api, CALL_ID)).error).toMatch(/did not reopen/);
  });
});

describe('what is said for a reopen that failed', () => {
  it("says a named refusal in the server's words, or in its own when the server gives none", () => {
    expect(describeReopenFailure({ errors: [{ errorType: 'Unauthorized', message: 'Not yours.' }] })).toBe(
      'Not yours.',
    );
    expect(describeReopenFailure({ errors: [{ errorType: 'TranscriptDeleted' }] })).toMatch(/transcript has been/);
  });

  it('says what went wrong otherwise', () => {
    expect(describeReopenFailure({ errors: [{ errorType: 'Unknown', message: 'boom' }] })).toBe(
      'LMA could not reopen the meeting: boom',
    );
    expect(describeReopenFailure(new Error('Network error'))).toBe('LMA could not reopen the meeting: Network error');
    expect(describeReopenFailure(null)).toBe('LMA could not reopen the meeting.');
  });
});
