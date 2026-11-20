/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * "Continue an existing meeting…": capture into a meeting that already exists,
 * even one shown as Done in LMA, on the same transcript timeline.
 *
 * Before streaming, the extension asks LMA's AppSync API to reopen the meeting
 * (the `reopenMeeting` mutation), with the signed-in user's Cognito token. The
 * server checks that the user is the meeting's owner or an Admin, that the
 * meeting is not still being captured and that its transcript has not been
 * deleted, and answers with the next capture session's number. The extension
 * then streams with the meeting's ID as its callId and names that session in
 * its START frame (`sessionNumber`), as the Desktop Capture App and the Stream
 * Audio page do. See docs/websocket-streaming-api.md#continuing-an-existing-meeting.
 */

export type MeetingSession = { callId: string, sessionNumber: number, offsetSeconds: number };
export type ReopenResult = { session: MeetingSession, error?: undefined } | { session?: undefined, error: string };

export const REOPEN_MUTATION =
  'mutation ReopenMeeting($input: ReopenMeetingInput!) { ' +
  'reopenMeeting(input: $input) { CallId SessionNumber SessionOffsetSeconds ReopenedAt } }';

const MEETING_PAGE_MARKER = '#/calls/';

/**
 * The meeting ID in what the user pasted: the ID itself, or the link to the
 * meeting's page in LMA (`https://…/#/calls/<encoded meeting ID>`, with or
 * without a query or a trailing slash). Null when there is nothing usable.
 */
export function meetingIdFrom(input: string | null | undefined): string | null {
  const text = String(input ?? '').trim();
  if (!text) return null;
  const at = text.indexOf(MEETING_PAGE_MARKER);
  if (at >= 0) {
    const encoded = text.slice(at + MEETING_PAGE_MARKER.length).split('?')[0].replace(/\/+$/, '');
    let decoded = encoded;
    try {
      decoded = decodeURIComponent(encoded);
    } catch {
      // A link with a stray % is taken as it is.
    }
    return decoded.trim() || null;
  }
  if (/^https?:\/\//i.test(text)) return null;
  return text;
}

export const INVALID_MEETING_ID = "Paste the meeting ID, or the link to the meeting's page in LMA.";
export const NOT_CONFIGURED =
  'This copy of the extension cannot continue meetings: its configuration has no LMA API endpoint. ' +
  'Download the extension again from LMA.';

// Said when the server names a refusal but gives no message.
const REFUSAL_FALLBACKS: Record<string, string> = {
  MeetingNotFound: 'There is no meeting with this ID.',
  Unauthorized: "Only the meeting's owner or an Admin can continue this meeting.",
  TranscriptDeleted: "This meeting's transcript has been deleted, so it cannot be continued.",
  MeetingInProgress: 'This meeting is still being captured. Continue it once it shows as Done.',
  MeetingChanged: 'The meeting changed while it was being reopened. Try again.',
  BadRequest: INVALID_MEETING_ID,
};

type Json = Record<string, unknown>;
const asObject = (value: unknown): Json | null =>
  value && typeof value === 'object' && !Array.isArray(value) ? (value as Json) : null;

/** The answer to the mutation, as AppSync gives it: the session, or what to tell the user. */
export function parseReopenResponse(response: unknown, status: number): ReopenResult {
  const body = asObject(response);
  const first = Array.isArray(body?.errors) ? asObject(body?.errors[0]) : null;
  if (first) {
    const type = typeof first.errorType === 'string' ? first.errorType : '';
    const message = typeof first.message === 'string' ? first.message : '';
    if (REFUSAL_FALLBACKS[type]) return { error: message || REFUSAL_FALLBACKS[type] };
    return { error: message ? `LMA could not reopen the meeting: ${message}` : 'LMA could not reopen the meeting.' };
  }
  const answer = asObject(asObject(body?.data)?.reopenMeeting);
  const sessionNumber = Number(answer?.SessionNumber);
  if (!answer || typeof answer.CallId !== 'string' || !Number.isInteger(sessionNumber) || sessionNumber < 2) {
    return { error: `LMA did not reopen the meeting (HTTP ${status}).` };
  }
  return {
    session: { callId: answer.CallId, sessionNumber, offsetSeconds: Number(answer.SessionOffsetSeconds) || 0 },
  };
}

/**
 * Ask LMA to reopen the meeting. `token` is the user's Cognito ID token (the
 * access token works as well).
 */
export async function reopenMeeting(
  endpoint: string | undefined,
  token: string | undefined,
  input: string,
  fetchFn: typeof fetch = fetch,
): Promise<ReopenResult> {
  if (!endpoint || !/^https:\/\//i.test(endpoint) || endpoint.includes('[placeholder]')) {
    return { error: NOT_CONFIGURED };
  }
  const callId = meetingIdFrom(input);
  if (!callId) return { error: INVALID_MEETING_ID };
  let response: Response;
  try {
    response = await fetchFn(endpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: token || '' },
      body: JSON.stringify({ query: REOPEN_MUTATION, variables: { input: { CallId: callId } } }),
    });
  } catch (error) {
    const detail = error instanceof Error ? error.message : '';
    return { error: `LMA could not be reached to reopen the meeting. ${detail}`.trim() };
  }
  let body: unknown = null;
  try {
    body = await response.json();
  } catch {
    return { error: `LMA answered with HTTP ${response.status}.` };
  }
  return parseReopenResponse(body, response.status);
}

/**
 * The text of a CONSENT_STATE frame in which the transcriber refused or
 * stopped capturing (`captureAllowed: false`), for example a START naming a
 * session that was not allowed (SESSION_NOT_ALLOWED); null for any other
 * frame. See docs/websocket-streaming-api.md#who-may-start-a-meeting-or-add-to-one.
 */
export function refusalMessage(data: unknown): string | null {
  if (typeof data !== 'string') return null;
  let frame: Json | null;
  try {
    frame = asObject(JSON.parse(data));
  } catch {
    return null;
  }
  if (!frame || frame.event !== 'CONSENT_STATE' || frame.captureAllowed !== false) return null;
  if (typeof frame.message === 'string' && frame.message) return frame.message;
  return typeof frame.code === 'string' && frame.code
    ? `LMA stopped capturing this meeting (${frame.code}).`
    : 'LMA stopped capturing this meeting.';
}
