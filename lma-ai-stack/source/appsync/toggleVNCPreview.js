import { util } from '@aws-appsync/utils';

/**
 * Request handler for toggleVNCPreview mutation
 * Stores VNC preview control events in DynamoDB
 * Supports both Cognito User Pools and IAM authentication
 */
export function request(ctx) {
  const { CallId, Show } = ctx.arguments.input;
  
  // Handle both Cognito and IAM auth
  let userEmail = 'unknown';
  if (ctx.identity.claims && ctx.identity.claims.email) {
    // Cognito User Pools auth
    userEmail = ctx.identity.claims.email;
  } else if (ctx.identity.userArn) {
    // IAM auth - extract from ARN or use username
    userEmail = ctx.identity.username || ctx.identity.userArn.split('/').pop() || 'lambda';
  }
  
  return {
    operation: 'PutItem',
    key: util.dynamodb.toMapValues({
      PK: `vnc#${CallId}`,
      SK: `control#${util.time.nowISO8601()}`
    }),
    attributeValues: util.dynamodb.toMapValues({
      CallId,
      Action: Show ? 'open' : 'close',
      Timestamp: util.time.nowISO8601(),
      Success: true,
      RequestedBy: userEmail,
      TTL: util.time.nowEpochSeconds() + 86400  // 24 hour TTL
    })
  };
}

/**
 * Response handler for toggleVNCPreview mutation
 * Returns the VNC preview control result
 */
export function response(ctx) {
  if (ctx.error) {
    util.error(ctx.error.message, ctx.error.type);
  }
  return ctx.result;
}