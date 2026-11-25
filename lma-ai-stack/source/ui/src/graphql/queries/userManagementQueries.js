/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
/* eslint-disable */

export const listUsers = /* GraphQL */ `
  query ListUsers {
    listUsers {
      users {
        username
        email
        role
        status
        enabled
        createdAt
        lastSignInAt
        lastActiveAt
      }
      lastSignInAvailable
    }
  }
`;

export const createUser = /* GraphQL */ `
  mutation CreateUser($input: CreateUserInput!) {
    createUser(input: $input) {
      username
      email
      role
      status
      enabled
      createdAt
    }
  }
`;

// The meetings the Create user form offers to invite the person to: the
// Virtual Participants the Admin can see, and the meetings of the last days.
export const listInvitableMeetings = /* GraphQL */ `
  query ListInvitableMeetings($startDateTime: AWSDateTime!, $endDateTime: AWSDateTime!) {
    listVirtualParticipants {
      id
      CallId
      meetingName
      status
      scheduledFor
      createdAt
    }
    listCallsDateRange(startDateTime: $startDateTime, endDateTime: $endDateTime, limit: 50) {
      Calls {
        CallId
        CreatedAt
        Status
      }
    }
  }
`;

export const setUserRole = /* GraphQL */ `
  mutation SetUserRole($input: SetUserRoleInput!) {
    setUserRole(input: $input) {
      username
      email
      role
      status
      enabled
      createdAt
    }
  }
`;

export const deleteUser = /* GraphQL */ `
  mutation DeleteUser($input: DeleteUserInput!) {
    deleteUser(input: $input) {
      username
      success
    }
  }
`;
