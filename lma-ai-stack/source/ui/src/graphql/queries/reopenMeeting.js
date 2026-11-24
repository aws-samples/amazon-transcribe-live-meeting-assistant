/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

// Allow one more capture session on an existing meeting, before streaming to
// continue it (docs/websocket-streaming-api.md#continuing-an-existing-meeting).
export default /* GraphQL */ `
  mutation ReopenMeeting($input: ReopenMeetingInput!) {
    reopenMeeting(input: $input) {
      CallId
      SessionNumber
      SessionOffsetSeconds
      ReopenedAt
    }
  }
`;
