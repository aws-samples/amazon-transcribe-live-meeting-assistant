/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Subscription resolver for VNC preview control events
 * Allows all authenticated users to receive VNC preview toggle events for their meetings
 */
export function request() {
  return { payload: null };
}

/**
 * Response handler for VNC preview toggle subscription
 * Filters events by CallId - users only receive events for meetings they're viewing
 */
export function response(ctx) {
  const { fieldName, variables } = ctx.info;
  
  console.debug(`Setting up VNC preview subscription for user ${ctx.identity.username}`);
  console.debug(`fieldName: ${fieldName}`);
  console.debug(`CallId filter: ${variables.CallId}`);
  
  // No additional filtering needed - CallId parameter already filters events
  // Users can only subscribe to meetings they have access to view
  
  return null;
}