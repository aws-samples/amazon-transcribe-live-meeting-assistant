/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import { util } from '@aws-appsync/utils';

/**
 * The second function of the updateMyMeetingNotificationState pipeline, after
 * meetingAccess.js has accepted the caller: reads the meeting's expiry, so the
 * user's view of its notifications expires with it as well as being deleted
 * with it.
 */

/**
 * @param {import('@aws-appsync/utils').Context} ctx the context
 * @returns {*} the request
 */
export function request(ctx) {
    const key = `c#${ctx.args.input.CallId}`;
    return {
        operation: 'GetItem',
        key: util.dynamodb.toMapValues({ PK: key, SK: key }),
        projection: { expression: '#ttl', expressionNames: { '#ttl': 'ExpiresAfter' } },
    };
}

/**
 * @param {import('@aws-appsync/utils').Context} ctx the context
 * @returns {*} nothing; the expiry is stashed
 */
export function response(ctx) {
    if (ctx.error) {
        util.error(ctx.error.message, ctx.error.type);
    }
    if (!ctx.result) {
        util.error(`Meeting ${ctx.args.input.CallId} was not found`, 'NotFound');
    }
    ctx.stash.meetingExpiresAfter = ctx.result.ExpiresAfter === undefined ? null : ctx.result.ExpiresAfter;
    return null;
}
