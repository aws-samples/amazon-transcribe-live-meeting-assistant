/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import { util } from '@aws-appsync/utils';

/**
 * The second function of the listMeetingNotifications pipeline, after
 * meetingAccess.js has accepted the caller against the meeting as it is now:
 * one page of the meeting's notifications, in the order they were first
 * published (their sort key starts with CreatedAt), or newest first.
 *
 * Category keeps one category. Since keeps the notifications created or
 * updated at or after it, so a reader that polls with the latest UpdatedAt it
 * has seen also gets updates made through a DedupKey. The partition's other
 * rows (dedup pointers "d#", rate counters "r#" and users' views "u#") are
 * outside the "n#" range read here.
 */

const DEFAULT_LIMIT = 100;
const MAX_LIMIT = 500;

/**
 * @param {import('@aws-appsync/utils').Context} ctx the context
 * @returns {*} the request
 */
export function request(ctx) {
    const args = ctx.args;
    let limit = args.Limit === null || args.Limit === undefined ? DEFAULT_LIMIT : args.Limit;
    limit = Math.max(1, Math.min(limit, MAX_LIMIT));
    const query = {
        operation: 'Query',
        query: {
            expression: 'PK = :pk AND begins_with(SK, :prefix)',
            expressionValues: util.dynamodb.toMapValues({ ':pk': `nt#${args.CallId}`, ':prefix': 'n#' }),
        },
        scanIndexForward: args.NewestFirst !== true,
        limit,
    };
    if (args.NextToken) {
        query.nextToken = args.NextToken;
    }
    const filters = [];
    const names = {};
    const values = {};
    if (args.Category) {
        filters.push('#category = :category');
        names['#category'] = 'Category';
        values[':category'] = args.Category;
    }
    if (args.Since) {
        filters.push('#updatedAt >= :since');
        names['#updatedAt'] = 'UpdatedAt';
        values[':since'] = args.Since;
    }
    if (filters.length > 0) {
        query.filter = {
            expression: filters.join(' AND '),
            expressionNames: names,
            expressionValues: util.dynamodb.toMapValues(values),
        };
    }
    return query;
}

function notification(row) {
    return {
        CallId: row.CallId,
        NotificationId: row.NotificationId,
        Category: row.Category,
        Type: row.Type,
        Severity: row.Severity,
        Title: row.Title,
        Body: row.Body,
        Refs: row.Refs,
        DedupKey: row.DedupKey,
        Actions: row.Actions,
        Source: row.Source,
        CreatedBy: row.CreatedBy,
        CreatedAt: row.CreatedAt,
        UpdatedAt: row.UpdatedAt,
        Revision: row.Revision,
        ExpiresAt: row.ExpiresAt,
    };
}

/**
 * @param {import('@aws-appsync/utils').Context} ctx the context
 * @returns {*} the page
 */
export function response(ctx) {
    if (ctx.error) {
        util.error(ctx.error.message, ctx.error.type);
    }
    const items = ctx.result.items || [];
    return { Items: items.map(notification), NextToken: ctx.result.nextToken };
}
