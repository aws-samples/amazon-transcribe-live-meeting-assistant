/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

// Owner and SharedWith are required by the subscription's access filter. The
// page fetches the new frame through getVideoKeyframes, which signs its links.
export default /* GraphQL */ `
  subscription OnAddVideoKeyframe($callId: ID) {
    onAddVideoKeyframe(CallId: $callId) {
      CallId
      FrameId
      Timestamp
      Owner
      SharedWith
    }
  }
`;
