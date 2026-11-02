/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

// Owner and SharedWith are required by the subscription's access filter. The
// page reads the record again through getMeetingConsent on each message, so
// only the version is selected here.
export default /* GraphQL */ `
  subscription OnUpdateMeetingConsent($callId: ID) {
    onUpdateMeetingConsent(CallId: $callId) {
      CallId
      Owner
      SharedWith
      Consent {
        Version
      }
    }
  }
`;
