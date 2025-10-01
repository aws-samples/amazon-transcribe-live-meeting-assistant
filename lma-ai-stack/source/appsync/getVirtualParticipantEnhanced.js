/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
/**
 * AppSync JavaScript Resolver for getVirtualParticipantEnhanced
 * Retrieves a single Virtual Participant with enhanced details
 */

import { util } from '@aws-appsync/utils';

export function request(ctx) {
  const { id } = ctx.arguments;
  
  return {
    operation: 'GetItem',
    key: util.dynamodb.toMapValues({ id }),
    consistentRead: true,
  };
}

export function response(ctx) {
  const { error, result } = ctx;
  
  if (error) {
    util.error(error.message, error.type);
  }
  
  if (!result) {
    return null;
  }
  
  // Transform DynamoDB item to GraphQL response
  const item = util.dynamodb.fromMapValues(result);
  
  // Ensure arrays are properly formatted
  if (item.statusHistory && typeof item.statusHistory === 'string') {
    try {
      item.statusHistory = JSON.parse(item.statusHistory);
    } catch (e) {
      item.statusHistory = [];
    }
  }
  
  if (item.connectionDetails && typeof item.connectionDetails === 'string') {
    try {
      item.connectionDetails = JSON.parse(item.connectionDetails);
    } catch (e) {
      item.connectionDetails = {};
    }
  }
  
  if (item.errorDetails && typeof item.errorDetails === 'string') {
    try {
      item.errorDetails = JSON.parse(item.errorDetails);
    } catch (e) {
      item.errorDetails = {};
    }
  }
  
  if (item.metrics && typeof item.metrics === 'string') {
    try {
      item.metrics = JSON.parse(item.metrics);
    } catch (e) {
      item.metrics = {};
    }
  }
  
  return item;
}
