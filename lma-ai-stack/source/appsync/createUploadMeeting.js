/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import { util } from '@aws-appsync/utils';

/**
 * AppSync JS pass-through resolver for the `createUploadMeeting` mutation.
 *
 * Mirrors the pattern used by `shareMeetings.js` / `deleteMeetings.js`:
 * forwards the full `ctx` to the Lambda and returns its result as-is, after
 * translating any Lambda error into a GraphQL error via `util.error`.
 *
 * @param {import('@aws-appsync/utils').Context} ctx
 * @returns {import('@aws-appsync/utils').LambdaRequest}
 */
export function request(ctx) {
  return {
    operation: 'Invoke',
    payload: ctx,
  };
}

/**
 * @param {import('@aws-appsync/utils').Context} ctx
 */
export function response(ctx) {
  const { result, error } = ctx;
  if (error) {
    util.error(error.message, error.type, result);
  }
  return result;
}
