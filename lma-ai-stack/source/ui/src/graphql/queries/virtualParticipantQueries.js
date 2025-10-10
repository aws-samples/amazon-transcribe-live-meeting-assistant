/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
// Query to get a single Virtual Participant with detailed information
export const getVirtualParticipant = /* GraphQL */ `
  query GetVirtualParticipant($id: ID!) {
    getVirtualParticipant(id: $id) {
      id
      meetingName
      meetingPlatform
      meetingId
      status
      createdAt
      updatedAt
      owner
      SharedWith
      statusHistory {
        status
        timestamp
        message
        errorDetails
        duration
        metadata
      }
      connectionDetails {
        joinAttempts
        successfulJoins
        lastJoinAttempt
        connectionDuration
        disconnectionReason
        networkLatency
        audioQuality
        connectionStability
      }
      errorDetails {
        errorCode
        errorMessage
        errorCategory
        troubleshootingSteps
        lastErrorAt
        errorCount
      }
      metrics {
        totalDuration
        timeToJoin
        uptime
        averageLatency
        transcriptSegments
        audioMinutes
        lastActivity
      }
      relatedCallId
      endedAt
      endedBy
      endReason
    }
  }
`;

// Query to list all Virtual Participants with enhanced details
export const listVirtualParticipantsDetailed = /* GraphQL */ `
  query ListVirtualParticipantsDetailed {
    listVirtualParticipants {
      id
      meetingName
      meetingPlatform
      meetingId
      status
      createdAt
      updatedAt
      owner
      SharedWith
      relatedCallId
    }
  }
`;

// Mutation to update Virtual Participant status with detailed message
export const updateVirtualParticipantStatus = /* GraphQL */ `
  mutation UpdateVirtualParticipantStatus($input: UpdateVirtualParticipantStatusInput!) {
    updateVirtualParticipantStatus(input: $input) {
      id
      status
      updatedAt
      statusHistory {
        status
        timestamp
        message
        errorDetails
      }
    }
  }
`;

// Mutation to end/stop a Virtual Participant
export const endVirtualParticipant = /* GraphQL */ `
  mutation EndVirtualParticipant($input: EndVirtualParticipantInput!) {
    endVirtualParticipant(input: $input) {
      id
      status
      updatedAt
      endedAt
      endedBy
      endReason
    }
  }
`;

// Subscription for real-time Virtual Participant updates with detailed info
export const onUpdateVirtualParticipantDetailed = /* GraphQL */ `
  subscription OnUpdateVirtualParticipant($id: ID!) {
    onUpdateVirtualParticipant(id: $id) {
      id
      status
      updatedAt
      statusHistory {
        status
        timestamp
        message
        errorDetails
        duration
        metadata
      }
      connectionDetails {
        joinAttempts
        successfulJoins
        lastJoinAttempt
        connectionDuration
        disconnectionReason
        networkLatency
        audioQuality
        connectionStability
      }
      errorDetails {
        errorCode
        errorMessage
        errorCategory
        troubleshootingSteps
        lastErrorAt
        errorCount
      }
      metrics {
        totalDuration
        timeToJoin
        uptime
        averageLatency
        transcriptSegments
        audioMinutes
        lastActivity
      }
    }
  }
`;

// Mutation to add status history entry
export const addVirtualParticipantStatusHistory = /* GraphQL */ `
  mutation AddVirtualParticipantStatusHistory($input: AddStatusHistoryInput!) {
    addVirtualParticipantStatusHistory(input: $input) {
      id
      statusHistory {
        status
        timestamp
        message
        errorDetails
      }
    }
  }
`;

// Query to get Virtual Participant connection metrics
export const getVirtualParticipantMetrics = /* GraphQL */ `
  query GetVirtualParticipantMetrics($id: ID!) {
    getVirtualParticipantMetrics(id: $id) {
      id
      totalDuration
      connectionAttempts
      successfulConnections
      failureReasons
      averageJoinTime
      lastActivity
    }
  }
`;
