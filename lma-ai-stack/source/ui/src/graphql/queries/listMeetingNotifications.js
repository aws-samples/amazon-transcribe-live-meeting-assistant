/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

// A meeting's notifications in the order they were first published, a page at
// a time. Answered to the people getCall answers for the meeting.
export default /* GraphQL */ `
  query ListMeetingNotifications(
    $callId: ID!
    $category: String
    $since: AWSDateTime
    $limit: Int
    $nextToken: String
    $newestFirst: Boolean
  ) {
    listMeetingNotifications(
      CallId: $callId
      Category: $category
      Since: $since
      Limit: $limit
      NextToken: $nextToken
      NewestFirst: $newestFirst
    ) {
      Items {
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
      }
      NextToken
    }
  }
`;
