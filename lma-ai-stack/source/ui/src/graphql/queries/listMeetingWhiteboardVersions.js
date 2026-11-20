/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

// A whiteboard's versions, newest first, a page at a time.
export default /* GraphQL */ `
  query ListMeetingWhiteboardVersions($callId: ID!, $whiteboardId: ID!, $limit: Int, $nextToken: String) {
    listMeetingWhiteboardVersions(CallId: $callId, WhiteboardId: $whiteboardId, Limit: $limit, NextToken: $nextToken) {
      NextToken
      Items {
        CallId
        WhiteboardId
        Title
        Version
        Source
        ChangedPanels
        UpdatedBy
        UpdatedAt
      }
    }
  }
`;
