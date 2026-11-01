/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import { util } from '@aws-appsync/utils';

/**
 * The getCall access rule for an operation that names a meeting by CallId:
 * the operation is answered to a signed-in caller who is an Admin, the
 * meeting's owner or an entry of its SharedWith, and to a backend role with
 * IAM credentials. Anyone else is refused.
 *
 * Used as the resolver of the subscriptions whose messages carry no Owner or
 * SharedWith, so they cannot be filtered on them as subscription.js filters
 * meeting messages -- onSendChatMessage, onAddChatToken and onVNCPreviewToggle.
 * The meeting is read when the subscription is made; messages then reach it
 * through the CallId argument, which AppSync applies as a filter.
 *
 * Also the first function of the toggleVNCPreview pipeline, which names the
 * meeting as input.CallId: the write that follows runs only for a caller
 * accepted here.
 *
 * A signed-in caller who is not an Admin has to name the meeting.
 */

// A Lambda calling with IAM credentials arrives as an assumed role; a signed-in
// user arrives through Cognito. Same detection as getCall.response.vtl.
function isBackendRole(identity) {
    return typeof identity?.userArn === 'string' && identity.userArn.includes(':assumed-role/');
}

function isAdmin(identity) {
    return Array.isArray(identity?.groups) && identity.groups.includes('Admin');
}

/**
 * @param {import('@aws-appsync/utils').Context} ctx the context
 * @returns {*} the request
 */
export function request(ctx) {
    if (isBackendRole(ctx.identity) || isAdmin(ctx.identity)) {
        runtime.earlyReturn(null);
    }
    // A subscription names the meeting as an argument; a mutation in its input.
    const callId = ctx.args.CallId || (ctx.args.input && ctx.args.input.CallId);
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
 * @param {import('@aws-appsync/utils').Context} ctx the context
 * @returns {*} null: a subscription resolver returns no data, and the pipeline
 *   function that follows in toggleVNCPreview reads its own arguments
 */
export function response(ctx) {
    if (ctx.error) {
        util.error(ctx.error.message, ctx.error.type);
    }
    const meeting = ctx.result;
    const { username } = ctx.identity;
    const allowed =
        !!meeting &&
        typeof username === 'string' &&
        username.length > 0 &&
        (meeting.Owner === username || sharedWithEntries(meeting.SharedWith).includes(username));
    if (!allowed) {
        util.unauthorized();
    }
    return null;
}
