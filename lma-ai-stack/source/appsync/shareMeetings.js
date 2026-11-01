/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import { util } from '@aws-appsync/utils';

/**
 * Sends a request to a Lambda function. Passes all information about the request from the `info` object.
 * @param {import('@aws-appsync/utils').Context} ctx the context
 * @returns {import('@aws-appsync/utils').LambdaRequest} the request
 */
export function request(ctx) {
    return {
        operation: 'Invoke',
        payload: ctx
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

/**
 * Process a Lambda function response
 * @param {import('@aws-appsync/utils').Context} ctx the context
 * @returns {*} the Lambda function response
 */
export function response(ctx) {
    const { result, error } = ctx;
    if (error) {
        util.error(error.message, error.type, result);
    }
    // Published to the onShareMeetings subscription, filtered on SharedWith in
    // subscription.js; the Lambda returns the recipients as they were typed.
    if (result && result.SharedWith !== undefined && result.SharedWith !== null) {
        result.SharedWith = publishedSharedWith(result.SharedWith);
    }
    return result;
}
