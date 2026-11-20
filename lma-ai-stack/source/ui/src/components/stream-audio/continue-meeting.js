/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * "Continue an existing meeting…" on the Stream Audio page: capture into a
 * meeting that already exists, even one shown as Done, on the same transcript
 * timeline.
 *
 * Before streaming, the page calls reopenMeeting, which allows the meeting's
 * next capture session for its owner or an Admin; it then streams with the
 * meeting's ID as its callId and names that session in its START frame
 * (`sessionNumber`), as the Desktop Capture App does. See
 * docs/websocket-streaming-api.md#continuing-an-existing-meeting.
 */
import reopenMeetingMutation from '../../graphql/queries/reopenMeeting';

const MEETING_PAGE_MARKER = '#/calls/';

/**
 * The meeting ID in what the user pasted: the ID itself, or the link to the
 * meeting's page in LMA (`https://…/#/calls/<encoded meeting ID>`, with or
 * without a query or a trailing slash). Null when there is nothing usable.
 */
export const meetingIdFrom = (input) => {
  const text = String(input ?? '').trim();
  if (!text) return null;
  const at = text.indexOf(MEETING_PAGE_MARKER);
  if (at >= 0) {
    const encoded = text
      .slice(at + MEETING_PAGE_MARKER.length)
      .split('?')[0]
      .replace(/\/+$/, '');
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
};

/** What the user is told when the meeting ID cannot be read. */
export const INVALID_MEETING_ID = "Paste the meeting ID, or the link to the meeting's page in LMA.";

// Said when the server names a refusal but gives no message.
const REFUSAL_FALLBACKS = {
  MeetingNotFound: 'There is no meeting with this ID.',
  Unauthorized: "Only the meeting's owner or an Admin can continue this meeting.",
  TranscriptDeleted: "This meeting's transcript has been deleted, so it cannot be continued.",
  MeetingInProgress: 'This meeting is still being captured. Continue it once it shows as Done.',
  MeetingChanged: 'The meeting changed while it was being reopened. Try again.',
  BadRequest: INVALID_MEETING_ID,
};

/**
 * What the user is told for a reopenMeeting that failed: the server's own
 * message for the refusals it names, and otherwise what went wrong.
 */
export const describeReopenFailure = (error) => {
  const first = Array.isArray(error?.errors) ? error.errors[0] : null;
  const type = first?.errorType || '';
  const message = first?.message || '';
  if (REFUSAL_FALLBACKS[type]) return message || REFUSAL_FALLBACKS[type];
  const detail = message || error?.message || '';
  return detail ? `LMA could not reopen the meeting: ${detail}` : 'LMA could not reopen the meeting.';
};

/**
 * Ask LMA to reopen the meeting. Answers { session: { callId, sessionNumber,
 * offsetSeconds } } or { error } with the text to show.
 */
export const reopenMeeting = async (client, input) => {
  const callId = meetingIdFrom(input);
  if (!callId) return { error: INVALID_MEETING_ID };
  try {
    const result = await client.graphql({ query: reopenMeetingMutation, variables: { input: { CallId: callId } } });
    const answer = result?.data?.reopenMeeting;
    const sessionNumber = Number(answer?.SessionNumber);
    if (!answer?.CallId || !Number.isInteger(sessionNumber) || sessionNumber < 2) {
      return { error: 'LMA did not reopen the meeting.' };
    }
    return {
      session: {
        callId: answer.CallId,
        sessionNumber,
        offsetSeconds: Number(answer.SessionOffsetSeconds) || 0,
      },
    };
  } catch (error) {
    return { error: describeReopenFailure(error) };
  }
};

/**
 * The codes with which the transcriber refuses a START that names an existing
 * meeting (docs/websocket-streaming-api.md#who-may-start-a-meeting-or-add-to-one).
 * Their message is shown with the form, whether or not a consent step is.
 */
export const MEETING_REFUSALS = new Set([
  'MEETING_NOT_OWNED',
  'MEETING_NOT_FOUND',
  'SESSION_NOT_ALLOWED',
  'TRANSCRIPT_DELETED',
  'MEETING_NOT_CHECKED',
]);
