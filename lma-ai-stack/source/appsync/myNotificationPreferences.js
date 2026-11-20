/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import { util } from '@aws-appsync/utils';

/**
 * getMyNotificationPreferences and updateMyNotificationPreferences: the
 * categories the signed-in user has muted on every meeting. One row per user
 * (PK "usr#<username>", SK "notification-prefs"), keyed by the caller's
 * identity, never by an argument. The user management function deletes it
 * with the user.
 */

const MAX_MUTED = 50;
const MAX_PER_CHANGE = 50;
const MAX_CATEGORY = 32;
const CATEGORY_CHARS = 'abcdefghijklmnopqrstuvwxyz0123456789_-';

function key(ctx) {
    const name = ctx.identity && ctx.identity.username;
    if (typeof name !== 'string' || name.length === 0) {
        util.unauthorized();
    }
    return util.dynamodb.toMapValues({ PK: `usr#${name}`, SK: 'notification-prefs' });
}

function given(list) {
    return Array.isArray(list) && list.length > 0;
}

function checkList(list, field) {
    if (!given(list)) {
        return;
    }
    if (list.length > MAX_PER_CHANGE) {
        util.error(`At most ${MAX_PER_CHANGE} entries in ${field}`, 'BadRequest');
    }
    list.forEach((entry) => {
        if (
            typeof entry !== 'string' ||
            entry.length === 0 ||
            entry.length > MAX_CATEGORY ||
            !entry.split('').every((character) => CATEGORY_CHARS.includes(character))
        ) {
            util.error(`Each entry of ${field} is a category name`, 'BadRequest');
        }
    });
}

/**
 * @param {import('@aws-appsync/utils').Context} ctx the context
 * @returns {*} the request
 */
export function request(ctx) {
    if (ctx.info.fieldName !== 'updateMyNotificationPreferences') {
        return { operation: 'GetItem', key: key(ctx), consistentRead: true };
    }
    const { input } = ctx.args;
    checkList(input.Mute, 'Mute');
    checkList(input.Unmute, 'Unmute');
    if (given(input.Mute) === given(input.Unmute)) {
        util.error('Give Mute or Unmute', 'BadRequest');
    }
    const names = { '#muted': 'MutedCategories', '#updatedAt': 'UpdatedAt' };
    const values = { ':now': util.dynamodb.toDynamoDB(util.time.nowISO8601()) };
    let expression = 'SET #updatedAt = :now';
    const request = { operation: 'UpdateItem', key: key(ctx) };
    if (given(input.Mute)) {
        values[':mute'] = util.dynamodb.toStringSet(input.Mute);
        expression = `${expression} ADD #muted :mute`;
        request.condition = {
            expression: 'attribute_not_exists(#muted) OR size(#muted) < :max',
            expressionNames: { '#muted': 'MutedCategories' },
            // Room for every category this change adds, so the set never passes the cap.
            expressionValues: { ':max': util.dynamodb.toDynamoDB(MAX_MUTED - input.Mute.length + 1) },
        };
    } else {
        values[':unmute'] = util.dynamodb.toStringSet(input.Unmute);
        expression = `${expression} DELETE #muted :unmute`;
    }
    request.update = { expression, expressionNames: names, expressionValues: values };
    return request;
}

/**
 * @param {import('@aws-appsync/utils').Context} ctx the context
 * @returns {*} the user's preferences
 */
export function response(ctx) {
    if (ctx.error) {
        if (ctx.error.type === 'DynamoDB:ConditionalCheckFailedException') {
            util.error(`At most ${MAX_MUTED} categories can be muted`, 'LimitExceeded');
        }
        util.error(ctx.error.message, ctx.error.type);
    }
    const row = ctx.result || {};
    return { MutedCategories: row.MutedCategories || [] };
}
