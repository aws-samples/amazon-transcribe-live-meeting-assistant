/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import { util } from '@aws-appsync/utils';

/**
 * Hand the whole resolver context to the summary regenerator Lambda.
 *
 * The context, not just the arguments: the Lambda has to know who is asking.
 * This is a Lambda resolver, so there is no DynamoDB condition to carry the
 * "Admin, owner, or shared-with" rule the way the direct summary write does,
 * and the Lambda applies it from `identity` instead.
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
