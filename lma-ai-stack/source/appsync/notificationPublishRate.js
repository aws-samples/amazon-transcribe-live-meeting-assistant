/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import { util } from '@aws-appsync/utils';

/**
 * The second function of the publishMeetingNotification pipeline: counts the
 * publish against the meeting's allowance for the current UTC minute, and
 * refuses it with RateLimited once RATE_PER_MINUTE have been made. Inserts and
 * updates both count. The counter is a row of the meeting's notification
 * partition (SK "r#<minute>") that expires an hour later.
 */

const RATE_PER_MINUTE = 120;
const COUNTER_TTL_SECONDS = 3600;

/**
 * @param {import('@aws-appsync/utils').Context} ctx the context
 * @returns {*} the request
 */
export function request(ctx) {
    const now = util.time.nowEpochSeconds();
    const minute = Math.floor(now / 60);
    return {
        operation: 'UpdateItem',
        key: util.dynamodb.toMapValues({ PK: `nt#${ctx.args.input.CallId}`, SK: `r#${minute}` }),
        update: {
            expression: 'SET #ttl = if_not_exists(#ttl, :ttl) ADD #count :one',
            expressionNames: { '#ttl': 'ExpiresAfter', '#count': 'Count' },
            expressionValues: util.dynamodb.toMapValues({ ':ttl': now + COUNTER_TTL_SECONDS, ':one': 1 }),
        },
        condition: {
            expression: 'attribute_not_exists(#count) OR #count < :limit',
            expressionNames: { '#count': 'Count' },
            expressionValues: util.dynamodb.toMapValues({ ':limit': RATE_PER_MINUTE }),
        },
    };
}

/**
 * @param {import('@aws-appsync/utils').Context} ctx the context
 * @returns {*} nothing
 */
export function response(ctx) {
    if (ctx.error) {
        if (ctx.error.type === 'DynamoDB:ConditionalCheckFailedException') {
            util.error(
                `Meeting ${ctx.args.input.CallId} has had ${RATE_PER_MINUTE} notifications this minute; try again in a minute`,
                'RateLimited',
            );
        }
        util.error(ctx.error.message, ctx.error.type);
    }
    return null;
}
