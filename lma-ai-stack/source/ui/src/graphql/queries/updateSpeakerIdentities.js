/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Write the whole speaker identity map for a meeting.
 *
 * The map is sent in full rather than as a patch, so two people editing names on
 * the same meeting at once is last-write-wins. The returned Call feeds the
 * onUpdateCall subscription, which is how every other open client re-labels its
 * transcript without polling -- so the selection set must include
 * SpeakerIdentities.
 */
export default /* GraphQL */ `
  mutation UpdateSpeakerIdentities($input: UpdateSpeakerIdentitiesInput!) {
    updateSpeakerIdentities(input: $input) {
      CallId
      SpeakerIdentities
    }
  }
`;
