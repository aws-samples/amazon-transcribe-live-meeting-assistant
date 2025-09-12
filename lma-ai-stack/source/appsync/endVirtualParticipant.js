/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
/**
 * AppSync JavaScript Resolver for endVirtualParticipant
 * Ends a Virtual Participant and triggers Lambda for enhanced processing
 */

import { util } from '@aws-appsync/utils';

export function request(ctx) {
  const { input } = ctx.arguments;
  
  // Add debugging to see if this resolver is being called
  console.log('=== APPSYNC JS RESOLVER CALLED ===');
  console.log('Input:', JSON.stringify(input));
  console.log('Identity:', JSON.stringify(ctx.identity));
  
  // Invoke Lambda function for enhanced VP management
  return {
    operation: 'Invoke',
    payload: {
      operation: 'endVirtualParticipant',
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
    util.error(result.body.error || 'Failed to end Virtual Participant', 'InternalError');
  }
  
  return result.body;
}
