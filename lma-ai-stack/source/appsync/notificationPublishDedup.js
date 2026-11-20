/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import { util, runtime } from '@aws-appsync/utils';

/**
 * The third function of the publishMeetingNotification pipeline: for a
 * notification with a DedupKey, finds the notification the first publish with
 * that Category and DedupKey made, so the write updates it instead of adding
 * another.
 *
 * The pointer row (SK "d#<Category>#<DedupKey>") is read and, the first time,
 * created in one UpdateItem: if_not_exists keeps the id and sort key it already
 * holds, so of two publishes racing to create it, both end up writing the same
 * notification. Without a DedupKey this function does nothing.
 */

/**
 * @param {import('@aws-appsync/utils').Context} ctx the context
 * @returns {*} the request
 */
export function request(ctx) {
    const { input } = ctx.args;
    if (input.DedupKey === null || input.DedupKey === undefined || input.DedupKey === '') {
        return runtime.earlyReturn(null);
    }
    const expiresAfter = ctx.stash.notificationExpiresAfter;
    const values = {
        ':id': ctx.stash.notificationId,
        ':sk': ctx.stash.notificationSK,
        ':category': input.Category,
    };
    let expression = 'SET #id = if_not_exists(#id, :id), #sk = if_not_exists(#sk, :sk), #category = :category';
    const names = { '#id': 'NotificationId', '#sk': 'NotificationSK', '#category': 'Category', '#ttl': 'ExpiresAfter' };
    // The same expiry as the notification, set or removed as the write does, so
    // the pointer never expires before the notification it finds.
    if (expiresAfter !== null && expiresAfter !== undefined) {
        expression = ctx.stash.notificationTranscriptBound
            ? `${expression}, #ttl = if_not_exists(#ttl, :ttl)`
            : `${expression}, #ttl = :ttl`;
        values[':ttl'] = expiresAfter;
    } else {
        expression = `${expression} REMOVE #ttl`;
    }
    return {
        operation: 'UpdateItem',
        key: util.dynamodb.toMapValues({
            PK: `nt#${input.CallId}`,
            SK: `d#${input.Category}#${input.DedupKey}`,
        }),
        update: {
            expression,
            expressionNames: names,
            expressionValues: util.dynamodb.toMapValues(values),
        },
    };
}

/**
 * @param {import('@aws-appsync/utils').Context} ctx the context
 * @returns {*} nothing; the notification to write is stashed
 */
export function response(ctx) {
    if (ctx.error) {
        util.error(ctx.error.message, ctx.error.type);
    }
    ctx.stash.notificationId = ctx.result.NotificationId;
    ctx.stash.notificationSK = ctx.result.NotificationSK;
    return null;
}
