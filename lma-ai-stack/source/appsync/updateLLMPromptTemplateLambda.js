/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * AppSync Lambda resolver for updateLLMPromptTemplate mutation
 *
 * Delegates to a Lambda function, which validates the input against the allowed
 * field list and re-checks the caller's group membership. `ctx.identity` is
 * forwarded so the function reads the caller's groups from the validated token
 * rather than from the mutation arguments.
 */

import { util } from '@aws-appsync/utils';

export function request(ctx) {
  return {
    operation: 'Invoke',
    payload: {
      arguments: ctx.arguments,
      identity: ctx.identity
    }
  };
}

export function response(ctx) {
  if (ctx.error) {
    util.error(ctx.error.message, ctx.error.type);
  }
  return ctx.result;
}
