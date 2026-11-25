/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
/**
 * A meeting's participant list against the user pool, for the meeting's owner
 * and Admins: whether each person has an LMA account (none, or its role), and
 * Participant accounts for the people who have none. The meeting is named by
 * CallId, or by VirtualParticipantId before the Virtual Participant has created
 * it. See docs/user-management.md#participant-accounts-for-a-meeting.
 */
export const getMeetingParticipantAccounts = /* GraphQL */ `
  query GetMeetingParticipantAccounts($CallId: ID, $VirtualParticipantId: ID) {
    getMeetingParticipantAccounts(CallId: $CallId, VirtualParticipantId: $VirtualParticipantId) {
      name
      email
      account
    }
  }
`;

export const createParticipantAccounts = /* GraphQL */ `
  mutation CreateParticipantAccounts($input: CreateParticipantAccountsInput!) {
    createParticipantAccounts(input: $input) {
      results {
        email
        outcome
        message
      }
      created
      remainingToday
    }
  }
`;
