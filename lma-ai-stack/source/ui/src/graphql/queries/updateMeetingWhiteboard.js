/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

// Change a whiteboard, from the version it was read at (BaseVersion).
// The answer is published to onMeetingWhiteboard.
export default /* GraphQL */ `
  mutation UpdateMeetingWhiteboard($input: UpdateMeetingWhiteboardInput!) {
    updateMeetingWhiteboard(input: $input) {
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
