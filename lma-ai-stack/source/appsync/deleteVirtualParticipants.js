/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
/**
 * AppSync JavaScript Resolver for deleteVirtualParticipants
 * Deletes one or more Virtual Participants. Active VPs are ended first
 * (ECS container termination, ALB cleanup, scheduled-event cancellation, etc.)
 * before the DynamoDB record is removed.
 */

import { util } from '@aws-appsync/utils';

export function request(ctx) {
  const { input } = ctx.arguments;

  console.log('=== deleteVirtualParticipants RESOLVER CALLED ===');
  console.log('Input:', JSON.stringify(input));

  return {
    operation: 'Invoke',
    payload: {
      operation: 'deleteVirtualParticipants',
      arguments: { input },
      identity: ctx.identity,
      source: ctx.source,
      request: ctx.request,
    },
  };
}

export function response(ctx) {
  const { error, result } = ctx;

  if (error) {
    util.error(error.message, error.type);
  }

  if (result.statusCode !== 200) {
    util.error(result.body.error || 'Failed to delete Virtual Participants', 'InternalError');
  }

  return result.body;
}
