/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

// A meeting's whiteboards in the order they were made, with the meeting's switch
// and the deployment's limits. Answered to the people getCall answers for the
// meeting. See docs/meeting-whiteboards.md.
export default /* GraphQL */ `
  query ListMeetingWhiteboards($callId: ID!) {
    listMeetingWhiteboards(CallId: $callId) {
      CallId
      WhiteboardsEnabled
      AssistantAvailable
      MaxWhiteboards
      Writable
      Items {
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
      }
    }
  }
`;
