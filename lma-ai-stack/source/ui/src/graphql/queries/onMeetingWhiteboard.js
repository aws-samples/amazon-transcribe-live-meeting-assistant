/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

// Every change to a meeting's whiteboards. Owner and SharedWith are required by
// the subscription's access filter (subscription.js).
export default /* GraphQL */ `
  subscription OnMeetingWhiteboard($callId: ID) {
    onMeetingWhiteboard(CallId: $callId) {
      CallId
      WhiteboardId
      Title
      Template
      Instructions
      AssistantUpdates
      Panels {
        PanelId
        Title
        Kind
        Content
        Source
        UpdatedBy
        UpdatedAt
      }
      Version
      CreatedBy
      CreatedAt
      UpdatedBy
      UpdatedAt
      Deleted
      Owner
      SharedWith
    }
  }
`;
