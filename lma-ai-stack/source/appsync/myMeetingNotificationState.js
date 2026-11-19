/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import { util } from '@aws-appsync/utils';

/**
 * The last function of the getMyMeetingNotificationState and
 * updateMyMeetingNotificationState pipelines, after meetingAccess.js has
 * accepted the caller for the meeting.
 *
 * The signed-in user's own view of the meeting's notifications: the ones they
 * dismissed and the categories they muted there. The row (PK "nt#<CallId>",
 * SK "u#<username>") is keyed by the caller's identity, never by an argument,
 * so a user reads and changes only their own view. It is deleted with the
 * meeting's other notification rows, and expires with the meeting.
 */

const MAX_DISMISSED = 1000;
const MAX_PER_CHANGE = 50;
const MAX_ID = 64;
const MAX_CATEGORY = 32;
const CATEGORY_CHARS = 'abcdefghijklmnopqrstuvwxyz0123456789_-';

function username(ctx) {
    const name = ctx.identity && ctx.identity.username;
    if (typeof name !== 'string' || name.length === 0) {
        util.unauthorized();
    }
    return name;
}

function key(ctx, callId) {
    return util.dynamodb.toMapValues({ PK: `nt#${callId}`, SK: `u#${username(ctx)}` });
}

function given(list) {
    return Array.isArray(list) && list.length > 0;
}

function checkList(list, field, max, allowed) {
    if (!given(list)) {
        return;
    }
    if (list.length > MAX_PER_CHANGE) {
        util.error(`At most ${MAX_PER_CHANGE} entries in ${field}`, 'BadRequest');
    }
    list.forEach((entry) => {
        if (typeof entry !== 'string' || entry.length === 0 || entry.length > max) {
            util.error(`Each entry of ${field} is 1 to ${max} characters`, 'BadRequest');
        }
        if (allowed && !entry.split('').every((character) => allowed.includes(character))) {
            util.error(`${field} names categories, which hold only ${allowed}`, 'BadRequest');
        }
    });
}

function updateRequest(ctx) {
    const { input } = ctx.args;
    checkList(input.Dismiss, 'Dismiss', MAX_ID, null);
    checkList(input.Restore, 'Restore', MAX_ID, null);
    checkList(input.Mute, 'Mute', MAX_CATEGORY, CATEGORY_CHARS);
    checkList(input.Unmute, 'Unmute', MAX_CATEGORY, CATEGORY_CHARS);
    if (given(input.Dismiss) && given(input.Restore)) {
        util.error('Give Dismiss or Restore, not both', 'BadRequest');
    }
    if (given(input.Mute) && given(input.Unmute)) {
        util.error('Give Mute or Unmute, not both', 'BadRequest');
    }
    const names = { '#callId': 'CallId', '#updatedAt': 'UpdatedAt' };
    const values = {
        ':callId': util.dynamodb.toDynamoDB(input.CallId),
        ':now': util.dynamodb.toDynamoDB(util.time.nowISO8601()),
    };
    const sets = ['#callId = :callId', '#updatedAt = :now'];
    const adds = [];
    const deletes = [];
    const expiresAfter = ctx.stash.meetingExpiresAfter;
    if (expiresAfter !== null && expiresAfter !== undefined) {
        names['#ttl'] = 'ExpiresAfter';
        values[':ttl'] = util.dynamodb.toDynamoDB(expiresAfter);
        sets.push('#ttl = :ttl');
    }
    let condition = null;
    if (given(input.Dismiss)) {
        names['#dismissed'] = 'Dismissed';
        values[':dismiss'] = util.dynamodb.toStringSet(input.Dismiss);
        adds.push('#dismissed :dismiss');
        condition = {
            expression: 'attribute_not_exists(#dismissed) OR size(#dismissed) < :max',
            expressionNames: { '#dismissed': 'Dismissed' },
            // Room for every id this change adds, so the set never passes the cap.
            expressionValues: { ':max': util.dynamodb.toDynamoDB(MAX_DISMISSED - input.Dismiss.length + 1) },
        };
    }
    if (given(input.Restore)) {
        names['#dismissed'] = 'Dismissed';
        values[':restore'] = util.dynamodb.toStringSet(input.Restore);
        deletes.push('#dismissed :restore');
    }
    if (given(input.Mute)) {
        names['#muted'] = 'MutedCategories';
        values[':mute'] = util.dynamodb.toStringSet(input.Mute);
        adds.push('#muted :mute');
    }
    if (given(input.Unmute)) {
        names['#muted'] = 'MutedCategories';
        values[':unmute'] = util.dynamodb.toStringSet(input.Unmute);
        deletes.push('#muted :unmute');
    }
    if (adds.length === 0 && deletes.length === 0) {
        util.error('Give at least one of Dismiss, Restore, Mute or Unmute', 'BadRequest');
    }
    let expression = `SET ${sets.join(', ')}`;
    if (adds.length > 0) {
        expression = `${expression} ADD ${adds.join(', ')}`;
    }
    if (deletes.length > 0) {
        expression = `${expression} DELETE ${deletes.join(', ')}`;
    }
    const request = {
        operation: 'UpdateItem',
        key: key(ctx, input.CallId),
        update: { expression, expressionNames: names, expressionValues: values },
    };
    if (condition) {
        request.condition = condition;
    }
    return request;
}

/**
 * @param {import('@aws-appsync/utils').Context} ctx the context
 * @returns {*} the request
 */
export function request(ctx) {
    if (ctx.info.fieldName === 'updateMyMeetingNotificationState') {
        return updateRequest(ctx);
    }
    return { operation: 'GetItem', key: key(ctx, ctx.args.CallId), consistentRead: true };
}

/**
 * @param {import('@aws-appsync/utils').Context} ctx the context
 * @returns {*} the user's view
 */
export function response(ctx) {
    if (ctx.error) {
        if (ctx.error.type === 'DynamoDB:ConditionalCheckFailedException') {
            util.error(`At most ${MAX_DISMISSED} dismissed notifications are kept per meeting`, 'LimitExceeded');
        }
        util.error(ctx.error.message, ctx.error.type);
    }
    const row = ctx.result || {};
    const callId = ctx.args.input ? ctx.args.input.CallId : ctx.args.CallId;
    return {
        CallId: callId,
        Dismissed: row.Dismissed || [],
        MutedCategories: row.MutedCategories || [],
    };
}
