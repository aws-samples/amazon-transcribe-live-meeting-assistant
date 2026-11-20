/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

// The signed-in user's own view of meeting notifications: what they dismissed
// and muted on one meeting, and the categories they muted on every meeting.

export const getMyMeetingNotificationState = /* GraphQL */ `
  query GetMyMeetingNotificationState($callId: ID!) {
    getMyMeetingNotificationState(CallId: $callId) {
      CallId
      Dismissed
      MutedCategories
    }
  }
`;

export const updateMyMeetingNotificationState = /* GraphQL */ `
  mutation UpdateMyMeetingNotificationState($input: UpdateMyMeetingNotificationStateInput!) {
    updateMyMeetingNotificationState(input: $input) {
      CallId
      Dismissed
      MutedCategories
    }
  }
`;

export const getMyNotificationPreferences = /* GraphQL */ `
  query GetMyNotificationPreferences {
    getMyNotificationPreferences {
      MutedCategories
    }
  }
`;

export const updateMyNotificationPreferences = /* GraphQL */ `
  mutation UpdateMyNotificationPreferences($input: UpdateMyNotificationPreferencesInput!) {
    updateMyNotificationPreferences(input: $input) {
      MutedCategories
    }
  }
`;
