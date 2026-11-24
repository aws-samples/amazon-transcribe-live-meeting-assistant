/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * "Continue an existing meeting…" in the extension: the meeting ID in what was
 * pasted, the reopenMeeting call and its answers, and the transcriber's
 * refusal frames.
 */

import {
  INVALID_MEETING_ID,
  NOT_CONFIGURED,
  meetingIdFrom,
  parseReopenResponse,
  refusalMessage,
  reopenMeeting,
} from './continue-meeting';

const CALL_ID = 'Weekly sync - 2026-11-19-10:00:00.000';
const ENDPOINT = 'https://abc.appsync-api.us-east-1.amazonaws.com/graphql';

const answering = (body: any, status = 200) =>
  jest.fn(async () => ({ status, json: async () => body }) as unknown as Response);

describe('the meeting ID in what was pasted', () => {
  it.each([
    [CALL_ID, CALL_ID],
    [`  ${CALL_ID}\n`, CALL_ID],
    ['https://d1.cloudfront.net/#/calls/Weekly%20sync%20-%202026', 'Weekly sync - 2026'],
    ['https://d1.cloudfront.net/#/calls/m%2F1/?tab=summary', 'm/1'],
    ['https://example.com/other', null],
    ['https://d1.cloudfront.net/#/calls/', null],
    ['', null],
    [undefined, null],
  ])('%j is %j', (input, expected) => {
    expect(meetingIdFrom(input as string)).toBe(expected);
  });
});

describe('reopening a meeting', () => {
  it('posts the mutation with the token and answers the session', async () => {
    const fetchFn = answering({
      data: { reopenMeeting: { CallId: CALL_ID, SessionNumber: 2, SessionOffsetSeconds: 317 } },
    });
    const result = await reopenMeeting(ENDPOINT, 'id-token', `https://x/#/calls/${encodeURIComponent(CALL_ID)}`, fetchFn);
    expect(result).toEqual({ session: { callId: CALL_ID, sessionNumber: 2, offsetSeconds: 317 } });
    const [url, init] = (fetchFn.mock.calls[0] as unknown) as [string, RequestInit];
    expect(url).toBe(ENDPOINT);
    expect((init.headers as Record<string, string>).Authorization).toBe('id-token');
    expect(JSON.parse(init.body as string).variables).toEqual({ input: { CallId: CALL_ID } });
  });

  it.each([undefined, '', '[placeholder]', 'http://insecure.example/graphql'])(
    'is not offered without an https endpoint (%j)',
    async (endpoint) => {
      const fetchFn = answering({});
      expect(await reopenMeeting(endpoint, 't', CALL_ID, fetchFn)).toEqual({ error: NOT_CONFIGURED });
      expect(fetchFn).not.toHaveBeenCalled();
    },
  );

  it('asks nothing of the server for nothing usable', async () => {
    const fetchFn = answering({});
    expect(await reopenMeeting(ENDPOINT, 't', 'https://example.com/x', fetchFn)).toEqual({ error: INVALID_MEETING_ID });
    expect(fetchFn).not.toHaveBeenCalled();
  });

  it("shows the server's refusal", async () => {
    const fetchFn = answering({
      data: { reopenMeeting: null },
      errors: [{ errorType: 'Unauthorized', message: "Only the meeting's owner or an Admin can continue this meeting." }],
    });
    expect(await reopenMeeting(ENDPOINT, 't', CALL_ID, fetchFn)).toEqual({
      error: "Only the meeting's owner or an Admin can continue this meeting.",
    });
  });

  it('says when LMA cannot be reached', async () => {
    const fetchFn = jest.fn(async () => {
      throw new Error('Failed to fetch');
    });
    expect((await reopenMeeting(ENDPOINT, 't', CALL_ID, fetchFn as unknown as typeof fetch)).error).toBe(
      'LMA could not be reached to reopen the meeting. Failed to fetch',
    );
  });

  it('says when the answer is not JSON', async () => {
    const fetchFn = jest.fn(async () => ({ status: 502, json: async () => { throw new Error('x'); } }) as unknown as Response);
    expect((await reopenMeeting(ENDPOINT, 't', CALL_ID, fetchFn)).error).toBe('LMA answered with HTTP 502.');
  });
});

describe('the answer to reopenMeeting', () => {
  it('says a named refusal in its own words when the server gives none', () => {
    expect(parseReopenResponse({ errors: [{ errorType: 'MeetingInProgress' }] }, 200).error).toMatch(/still being captured/);
  });

  it('says what went wrong for an error it does not name', () => {
    expect(parseReopenResponse({ errors: [{ errorType: 'Unknown', message: 'boom' }] }, 200).error).toBe(
      'LMA could not reopen the meeting: boom',
    );
  });

  it('takes an answer that names no later session as a failure', () => {
    expect(parseReopenResponse({ data: { reopenMeeting: { CallId: CALL_ID, SessionNumber: 1 } } }, 200).error).toMatch(
      /did not reopen/,
    );
  });
});

describe("the transcriber's refusal frames", () => {
  it('reads the message of a frame that stops capture', () => {
    const frame = { event: 'CONSENT_STATE', captureAllowed: false, code: 'SESSION_NOT_ALLOWED', message: 'Not reopened.' };
    expect(refusalMessage(JSON.stringify(frame))).toBe('Not reopened.');
    expect(refusalMessage(JSON.stringify({ ...frame, message: '' }))).toBe(
      'LMA stopped capturing this meeting (SESSION_NOT_ALLOWED).',
    );
  });

  it.each([
    JSON.stringify({ event: 'CONSENT_STATE', captureAllowed: true, message: 'ok' }),
    JSON.stringify({ event: 'CONSENT_SETTINGS' }),
    'not json',
    new ArrayBuffer(4),
  ])('ignores anything else', (data) => {
    expect(refusalMessage(data)).toBeNull();
  });
});
