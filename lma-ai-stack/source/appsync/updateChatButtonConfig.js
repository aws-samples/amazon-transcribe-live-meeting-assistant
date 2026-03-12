/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * AppSync resolver for updateChatButtonConfig mutation
 * Saves custom button configuration to DynamoDB
 * 
 * NOTE: AF-12 Mass Assignment security fix cannot be implemented in APPSYNC_JS 1.0.0
 * Testing shows that while Object.keys(), .filter(), and .reduce() are documented as supported,
 * they fail validation when combined with util.parseJson() and object manipulation.
 * 
 * SECURITY RISK: This resolver stores the ButtonConfig as-is without field filtering.
 * MITIGATION REQUIRED: Migrate to Lambda function resolver to implement proper input validation.
 */

import { util } from '@aws-appsync/utils';

export function request(ctx) {
  const ChatButtonConfigId = ctx.arguments.input.ChatButtonConfigId;
  const ButtonConfig = ctx.arguments.input.ButtonConfig;
  
  return {
    operation: 'PutItem',
    key: util.dynamodb.toMapValues({ ChatButtonConfigId }),
    attributeValues: util.dynamodb.toMapValues({ 
      ChatButtonConfigId,
      ButtonConfig
    })
  };
}

export function response(ctx) {
  if (ctx.error) {
    util.error(ctx.error.message, ctx.error.type);
  }
  return { 
    ChatButtonConfigId: ctx.arguments.input.ChatButtonConfigId, 
    Success: true 
  };
}
