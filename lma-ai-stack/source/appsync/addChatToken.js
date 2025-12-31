/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * AppSync resolver for addChatToken mutation
 * This is a passthrough resolver that broadcasts tokens to subscribers
 * No DynamoDB storage needed - tokens are ephemeral
 */

export function request(ctx) {
  // For NONE data source, just return empty object
  // The response function will construct the actual return value
  return {};
}

export function response(ctx) {
  // For NONE data source, construct and return the ChatToken from input
  const { input } = ctx.arguments;
  
  return {
    CallId: input.CallId,
    MessageId: input.MessageId,
    Token: input.Token,
    IsComplete: input.IsComplete,
    Sequence: input.Sequence,
    Timestamp: util.time.nowISO8601(),
    ThinkingStep: input.ThinkingStep || null
  };
}
