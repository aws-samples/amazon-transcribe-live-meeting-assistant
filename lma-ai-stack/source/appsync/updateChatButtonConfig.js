/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * AppSync resolver for updateChatButtonConfig mutation
 * Saves custom button configuration to DynamoDB
 */

import { util } from '@aws-appsync/utils';

export function request(ctx) {
  const { ChatButtonConfigId, ButtonConfig } = ctx.args.input;
  
  // Parse the ButtonConfig JSON string
  const configObject = JSON.parse(ButtonConfig);
  
  // Security: Allowlist only button configuration fields (format: N#LABEL for buttons)
  // This prevents mass assignment of unexpected DynamoDB attributes
  const allowedFields = {};
  const buttonPattern = /^\d+#/; // Matches button fields like "1#Action Items", "2#Summary", etc.
  
  Object.keys(configObject).forEach((key) => {
    if (buttonPattern.test(key)) {
      allowedFields[key] = configObject[key];
    }
  });
  
  // Build the item to store in DynamoDB - merge sanitized config fields with ID
  const item = Object.assign({ ChatButtonConfigId }, allowedFields);
  
  return {
    operation: 'PutItem',
    key: util.dynamodb.toMapValues({
      ChatButtonConfigId: ChatButtonConfigId
    }),
    attributeValues: util.dynamodb.toMapValues(item)
  };
}

export function response(ctx) {
  if (ctx.error) {
    util.error(ctx.error.message, ctx.error.type);
  }
  
  return {
    ChatButtonConfigId: ctx.args.input.ChatButtonConfigId,
    Success: true
  };
}
