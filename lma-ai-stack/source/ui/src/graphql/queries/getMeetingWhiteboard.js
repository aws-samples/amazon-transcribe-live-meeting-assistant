/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

// One whiteboard, or one earlier version of it when version is given.
export default /* GraphQL */ `
  query GetMeetingWhiteboard($callId: ID!, $whiteboardId: ID!, $version: Int) {
    getMeetingWhiteboard(CallId: $callId, WhiteboardId: $whiteboardId, Version: $version) {
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
      Source
      ChangedPanels
    }
  }
`;
