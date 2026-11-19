/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import { util } from '@aws-appsync/utils';

/**
 * The last function of the publishMeetingNotification pipeline: stores the
 * notification and answers with it, which AppSync publishes to
 * onMeetingNotification.
 *
 * One UpdateItem both creates a new notification and updates the one a DedupKey
 * found: CreatedAt is kept once set, Revision counts the publishes, and every
 * other field is replaced by this publish's value or removed when this publish
 * leaves it out. Owner and SharedWith are not stored. They are put on the answer
 * only, from the meeting as the first function read it, in the "[a, b]" form the
 * filters in subscription.js match.
 */

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

// BEGIN publishedSharedWith -- one text in every resolver that has it;
// test_shared_with_access.py holds the copies to it.
// SharedWith as "[a, b]", the form the "SharedWith published" block of the VTL
// templates gives it, so the filters in subscription.js see one shape. A value
// with no entries is returned as it is.
function publishedSharedWith(value) {
    const entries = sharedWithEntries(value);
    if (entries.length === 0) {
        return value;
    }
    return `[${entries.join(', ')}]`;
}
// END publishedSharedWith

// The fields a publish sets, and which of them it may leave out.
const REQUIRED_FIELDS = ['Category', 'Type', 'Severity', 'Source'];
const OPTIONAL_FIELDS = ['Title', 'Body', 'Refs', 'DedupKey', 'Actions', 'CreatedBy', 'ExpiresAt'];

function isAbsent(value) {
    return (
        value === null ||
        value === undefined ||
        value === '' ||
        (Array.isArray(value) && value.length === 0)
    );
}

/**
 * @param {import('@aws-appsync/utils').Context} ctx the context
 * @returns {*} the request
 */
export function request(ctx) {
    const { input } = ctx.args;
    const now = ctx.stash.notificationNow;
    const names = {
        '#callId': 'CallId',
        '#id': 'NotificationId',
        '#createdAt': 'CreatedAt',
        '#updatedAt': 'UpdatedAt',
        '#revision': 'Revision',
    };
    const values = {
        ':callId': input.CallId,
        ':id': ctx.stash.notificationId,
        ':now': now,
        ':one': 1,
    };
    const sets = ['#callId = :callId', '#id = :id', '#createdAt = if_not_exists(#createdAt, :now)', '#updatedAt = :now'];
    const removes = [];
    REQUIRED_FIELDS.forEach((field) => {
        names[`#${field}`] = field;
        values[`:${field}`] = input[field];
        sets.push(`#${field} = :${field}`);
    });
    OPTIONAL_FIELDS.forEach((field) => {
        names[`#${field}`] = field;
        if (isAbsent(input[field])) {
            removes.push(`#${field}`);
        } else {
            values[`:${field}`] = input[field];
            sets.push(`#${field} = :${field}`);
        }
    });
    names['#ttl'] = 'ExpiresAfter';
    const expiresAfter = ctx.stash.notificationExpiresAfter;
    if (expiresAfter === null || expiresAfter === undefined) {
        removes.push('#ttl');
    } else {
        values[':ttl'] = expiresAfter;
        sets.push('#ttl = :ttl');
    }
    let expression = `SET ${sets.join(', ')} ADD #revision :one`;
    if (removes.length > 0) {
        expression = `${expression} REMOVE ${removes.join(', ')}`;
    }
    return {
        operation: 'UpdateItem',
        key: util.dynamodb.toMapValues({ PK: `nt#${input.CallId}`, SK: ctx.stash.notificationSK }),
        update: {
            expression,
            expressionNames: names,
            expressionValues: util.dynamodb.toMapValues(values),
        },
    };
}

/**
 * @param {import('@aws-appsync/utils').Context} ctx the context
 * @returns {*} the notification, with the meeting's Owner and SharedWith
 */
export function response(ctx) {
    if (ctx.error) {
        util.error(ctx.error.message, ctx.error.type);
    }
    const row = ctx.result;
    const meeting = ctx.stash.notificationMeeting;
    const sharedWith = sharedWithEntries(meeting.SharedWith).length > 0 ? publishedSharedWith(meeting.SharedWith) : null;
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
        Created: row.Revision === 1,
        Owner: meeting.Owner,
        SharedWith: sharedWith,
    };
}
