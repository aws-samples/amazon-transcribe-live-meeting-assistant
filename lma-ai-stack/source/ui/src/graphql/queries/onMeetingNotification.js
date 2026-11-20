/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

// Notifications published to a meeting, or without callId to every meeting the
// subscriber may see. Owner and SharedWith are required by the subscription's
// access filter (subscription.js).
export default /* GraphQL */ `
  subscription OnMeetingNotification($callId: ID) {
    onMeetingNotification(CallId: $callId) {
      CallId
      NotificationId
      Category
      Type
      Severity
      Title
      Body
      Refs
      DedupKey
      Actions {
        Label
        Kind
        Payload
      }
      Source
      CreatedBy
      CreatedAt
      UpdatedAt
      Revision
      ExpiresAt
      Owner
      SharedWith
    }
  }
`;
