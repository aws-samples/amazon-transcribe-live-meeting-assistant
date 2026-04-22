/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
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
      }
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

export const deleteUser = /* GraphQL */ `
  mutation DeleteUser($input: DeleteUserInput!) {
    deleteUser(input: $input) {
      username
      success
    }
  }
`;
