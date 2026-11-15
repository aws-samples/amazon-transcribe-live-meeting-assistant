/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import { util } from '@aws-appsync/utils';

/**
 * Forwards startTroubleshooting and getTroubleshootingJob to the
 * troubleshooting resolver Lambda: the arguments, the verified identity and the
 * field name, which is what the Lambda dispatches on and what its access checks
 * read. The payload is built here, so it always carries `info`, which is how the
 * Lambda tells a request from its own worker invocation.
 * @param {import('@aws-appsync/utils').Context} ctx the context
 * @returns {import('@aws-appsync/utils').LambdaRequest} the request
 */
export function request(ctx) {
    return {
        operation: 'Invoke',
        payload: {
            arguments: ctx.arguments,
            identity: ctx.identity,
            info: { fieldName: ctx.info.fieldName },
        },
    };
}

/**
 * @param {import('@aws-appsync/utils').Context} ctx the context
 * @returns {*} the Lambda function response
 */
export function response(ctx) {
    if (ctx.error) {
        util.error(ctx.error.message, ctx.error.type);
    }
    return ctx.result;
}
