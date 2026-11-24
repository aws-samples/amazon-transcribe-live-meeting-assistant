/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Whether Meeting Recording › Record Virtual Participant meeting video is on:
 * true, false, or null when it could not be read. Read by every signed-in user.
 */
export default /* GraphQL */ `
  query GetVirtualParticipantVideoRecording {
    getVirtualParticipantVideoRecording
  }
`;
