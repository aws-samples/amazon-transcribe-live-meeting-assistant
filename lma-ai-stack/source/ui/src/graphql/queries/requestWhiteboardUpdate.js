/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

// Update the meeting's whiteboards from the transcript now: one model call.
export default /* GraphQL */ `
  mutation RequestWhiteboardUpdate($callId: ID!) {
    requestWhiteboardUpdate(CallId: $callId) {
      Success
      Message
      Updated
    }
  }
`;
