/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import { util } from '@aws-appsync/utils';

/**
 * Invoke the summary regenerator Lambda with the arguments and the identity.
 *
 * The identity as well as the arguments: the Lambda has to know who is asking.
 * This is a Lambda resolver, so there is no DynamoDB condition to carry the
 * "Admin, owner, or shared-with" rule the way the direct summary write does,
 * and the Lambda applies it from `identity` instead.
 *
 * Those two fields and no more: the payload names what the Lambda reads rather
 * than forwarding the context whole, so the invocation carries the data the
 * Lambda works from and nothing else the resolver happens to hold.
 *
 * @param {import('@aws-appsync/utils').Context} ctx the context
 * @returns {import('@aws-appsync/utils').LambdaRequest} the request
 */
export function request(ctx) {
    return {
        operation: 'Invoke',
        payload: { arguments: ctx.arguments, identity: ctx.identity },
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
