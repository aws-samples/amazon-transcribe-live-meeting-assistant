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

// BEGIN publishedSharedWith -- one text in every resolver that has it;
// test_shared_with_access.py holds the copies to it.
// SharedWith as "[a, b]", the form the "SharedWith published" block of the VTL
// templates gives it, so the filters in subscription.js see one shape. Reads a
// list, a comma-separated string and an "[a, b]" string; a value with no
// entries is returned as it is.
function publishedSharedWith(value) {
    let entries = [];
    if (Array.isArray(value)) {
        entries = value.filter((entry) => typeof entry === 'string');
    } else if (typeof value === 'string') {
        let text = value.trim();
        if (text.startsWith('[')) {
            text = text.slice(1);
        }
        if (text.endsWith(']')) {
            text = text.slice(0, -1);
        }
        entries = text.split(',');
    }
    entries = entries.map((entry) => entry.trim()).filter((entry) => entry.length > 0);
    if (entries.length === 0) {
        return value;
    }
    return `[${entries.join(', ')}]`;
}
// END publishedSharedWith

export function response(ctx) {
  const { error, result } = ctx;

  if (error) {
    util.error(error.message, error.type);
  }

  if (result.statusCode !== 200) {
    util.error(result.body.error || 'Failed to end Virtual Participant', 'InternalError');
  }

  // Published to the onUpdateVirtualParticipant subscription, filtered on
  // SharedWith in subscription.js; the row stores it as a comma-separated string.
  const body = result.body;
  if (body && body.SharedWith !== undefined && body.SharedWith !== null) {
    body.SharedWith = publishedSharedWith(body.SharedWith);
  }
  return body;
}
