/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import { util, extensions } from '@aws-appsync/utils';

/**
 * onAddParticipantTranscriptSegment: the live transcript of a meeting shared
 * with a Participant.
 *
 * When the subscription is made, the meeting it names is read and the caller
 * is admitted only when its SharedWith names them (being its owner is not
 * enough: a Participant reads shared meetings only), and only for a selection
 * of the fields a ParticipantTranscriptSegment has, so that the segment's
 * Owner, SharedWith and sentiment are never sent. Each message is then
 * filtered on the meeting, on the transcript's speech and meeting-chat
 * channels (not the meeting assistant's answers, which are the owner's), and
 * on the sharing list it is published with, so a meeting unshared while the
 * page is open stops reaching it with the next line. AppSync applies a filter
 * to the published message whatever the subscriber selected.
 *
 * AppSync accepts a filter of at most 10 filters, counting each value of an
 * `in` list as a filter of its own, and at most 5 fields to a filter (see
 * segmentFilter).
 */

// The fields a subscriber may select: ParticipantTranscriptSegment's.
const ALLOWED_FIELDS = [
  'CallId',
  'SegmentId',
  'StartTime',
  'EndTime',
  'Transcript',
  'IsPartial',
  'Channel',
  'Speaker',
  'LanguageCode',
  'CreatedAt',
  '__typename',
];

// The meeting assistant's channels: every value of the Channel enum other than
// what was said (the speech channels, under either spelling) and typed in the
// meeting chat, lma_channels.STORED_TRANSCRIPT_CHANNELS.
// test_participant_subscription_filter.py holds this list to the two.
const ASSISTANT_CHANNELS = ['AGENT_ASSISTANT', 'CHAT_ASSISTANT'];

/**
 * @param {import('@aws-appsync/utils').Context} ctx the context
 * @returns {*} the request
 */
export function request(ctx) {
  const callId = ctx.args.CallId;
  if (!callId) {
    util.unauthorized();
  }
  const key = `c#${callId}`;
  return {
    operation: 'GetItem',
    key: util.dynamodb.toMapValues({ PK: key, SK: key }),
    consistentRead: true,
  };
}

// BEGIN sharedWithEntries -- one text in every resolver that has it;
// test_shared_with_access.py holds the copies to it.
// The entries of a SharedWith value, trimmed and non-empty, read as the
// "SharedWith entries" block of the VTL templates reads them: from a list, a
// comma-separated string and an "[a, b]" string. "[", "]" and "," only ever
// separate entries: a string is split at each of them, and a list element that
// contains one is not an entry.
function sharedWithEntries(value) {
    let entries = [];
    if (Array.isArray(value)) {
        entries = value.filter(
            (entry) =>
                typeof entry === 'string' && !['[', ']', ','].some((separator) => entry.includes(separator)),
        );
    } else if (typeof value === 'string') {
        entries = value.split('[').join(',').split(']').join(',').split(',');
    }
    return entries.map((entry) => entry.trim()).filter((entry) => entry.length > 0);
}
// END sharedWithEntries

/**
 * The filter on each published segment: the meeting, a transcript channel,
 * and the caller an entry of the segment's SharedWith.
 *
 * Every segment is published with SharedWith as "[a, b]" (the "SharedWith
 * published" block of dDbPutCondition.response.vtl), so the caller is an entry
 * exactly when it is "[caller]", starts "[caller, ", or holds ", caller, " or
 * ", caller]", as subscription.js matches it. Those are four filters, each
 * with the meeting and the channel. The channel is held to the transcript's
 * as `notIn` the assistant's channels: Channel is an enum, so the two say the
 * same, and `notIn` is one filter where `in` would count each of its five
 * values, making 4 x 5 = 20. Four filters of three fields each, whatever the
 * caller and however long the sharing list.
 *
 * @param {string} callId the meeting
 * @param {string} username the caller
 * @returns {*} the filter, for util.transform.toSubscriptionFilter
 */
function segmentFilter(callId, username) {
  return {
    CallId: { eq: callId },
    Channel: { notIn: ASSISTANT_CHANNELS },
    or: [
      { SharedWith: { eq: `[${username}]` } },
      { SharedWith: { beginsWith: `[${username}, ` } },
      { SharedWith: { contains: `, ${username}, ` } },
      { SharedWith: { contains: `, ${username}]` } },
    ],
  };
}

/**
 * @param {import('@aws-appsync/utils').Context} ctx the context
 * @returns {*} null, as every subscription resolver returns
 */
export function response(ctx) {
  if (ctx.error) {
    util.error(ctx.error.message, ctx.error.type);
  }
  const { username, groups } = ctx.identity;
  const meeting = ctx.result;
  if (
    typeof username === 'string' &&
    username.length > 0 &&
    ['[', ']', ','].some((separator) => username.includes(separator))
  ) {
    util.unauthorized();
  }
  const admitted =
    Array.isArray(groups) &&
    groups.includes('Participant') &&
    !groups.includes('Admin') &&
    typeof username === 'string' &&
    username.length > 0 &&
    !!meeting &&
    sharedWithEntries(meeting.SharedWith).includes(username);
  if (!admitted) {
    util.unauthorized();
  }
  const selected = ctx.info.selectionSetList.filter((field) => !ALLOWED_FIELDS.includes(field));
  if (selected.length > 0) {
    util.error(`Not a field of a Participant's transcript: ${selected.join(', ')}`, 'Unauthorized');
  }
  const filter = segmentFilter(ctx.args.CallId, username);
  extensions.setSubscriptionFilter(util.transform.toSubscriptionFilter(filter));
  return null;
}
