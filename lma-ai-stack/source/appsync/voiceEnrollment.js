/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import { util } from '@aws-appsync/utils';

/**
 * Hand the whole resolver context to the voice-enrollment Lambda.
 *
 * The context, not just the arguments: every operation here turns on WHO is
 * asking. Only the enrollee can consent to their own voice being enrolled, and
 * the function derives that from `identity` -- so the identity has to travel
 * with the request rather than being asserted in an argument, which a caller
 * could set to anybody.
 *
 * `request.headers` travels too, because the consent record keeps the source IP
 * and user agent of the session the consent was given in.
 *
 * @param {import('@aws-appsync/utils').Context} ctx the context
 * @returns {import('@aws-appsync/utils').LambdaRequest} the request
 */
export function request(ctx) {
    return {
        operation: 'Invoke',
        payload: ctx,
    };
}

/**
 * @param {import('@aws-appsync/utils').Context} ctx the context
 * @returns {*} the Lambda function response
 */
export function response(ctx) {
    const { result, error } = ctx;
    if (error) {
        util.error(error.message, error.type, result);
    }
    return result;
}
