/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * A short digest of the meeting so far, made by live guidance and published to
 * the meeting's notifications (docs/live-guidance.md). Success false carries the
 * reason in Message.
 */
export default /* GraphQL */ `
  mutation RequestMeetingDigest($CallId: ID!) {
    requestMeetingDigest(CallId: $CallId) {
      Success
      Message
      Text
      NotificationId
    }
  }
`;
