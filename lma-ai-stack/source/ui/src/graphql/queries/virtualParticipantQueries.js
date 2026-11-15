/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
// Mutation to delete one or more Virtual Participants. Active VPs are ended
// server-side (ECS task stopped, ALB/schedule cleanup) before the DynamoDB
// record is removed.
export const deleteVirtualParticipants = /* GraphQL */ `
  mutation DeleteVirtualParticipants($input: DeleteVirtualParticipantsInput!) {
    deleteVirtualParticipants(input: $input) {
      Result
    }
  }
`;

// Query to list Virtual Participants with VNC fields for CallPanel preview, and the
// platform and video recording choice the meeting page shows
export const listVirtualParticipants = /* GraphQL */ `
  query ListVirtualParticipants {
    listVirtualParticipants {
      id
      CallId
      vncEndpoint
      vncPort
      vncReady
      status
      manualActionType
      manualActionMessage
      manualActionTimeoutSeconds
      manualActionStartTime
      meetingName
      meetingPlatform
      enableVideoRecording
    }
  }
`;

// Subscription for VNC preview updates
export const onUpdateVirtualParticipant = /* GraphQL */ `
  subscription OnUpdateVirtualParticipant {
    onUpdateVirtualParticipant {
      id
      CallId
      status
      vncEndpoint
      vncPort
      vncReady
      manualActionType
      manualActionMessage
      manualActionTimeoutSeconds
      manualActionStartTime
      Owner
      SharedWith
    }
  }
`;
