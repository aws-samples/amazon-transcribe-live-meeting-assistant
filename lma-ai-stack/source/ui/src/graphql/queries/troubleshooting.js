/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

// The troubleshooting agent's two operations. A diagnosis takes minutes, so it
// is a job: startTroubleshooting returns it at once, PENDING, and
// getTroubleshootingJob is read until its Status is COMPLETED or FAILED.
// Written out in full rather than sharing a fragment, so test_appsync_contract.py
// validates both against the schema.

export const startTroubleshooting = /* GraphQL */ `
  mutation StartTroubleshooting($input: StartTroubleshootingInput!) {
    startTroubleshooting(input: $input) {
      JobId
      Status
      Question
      CallId
      VirtualParticipantId
      Answer
      Error
      Truncated
      CreatedAt
      UpdatedAt
    }
  }
`;

export const getTroubleshootingJob = /* GraphQL */ `
  query GetTroubleshootingJob($JobId: ID!) {
    getTroubleshootingJob(JobId: $JobId) {
      JobId
      Status
      Question
      CallId
      VirtualParticipantId
      Answer
      Error
      Truncated
      CreatedAt
      UpdatedAt
    }
  }
`;
