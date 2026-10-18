/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Ask the backend to infer speaker names from what people said.
 *
 * The names are not in this response. The resolver writes them through
 * updateSpeakerIdentities, which publishes onUpdateCall, so they arrive by the
 * same route a rename by another user does and the transcript re-labels itself.
 * What comes back is only how many were applied, for the status message.
 *
 * `Force` is set when a user pressed the button: an automatic pass skips the
 * model when nothing new could name anybody, and a person who asked deserves an
 * answer either way.
 */
export default /* GraphQL */ `
  mutation ResolveSpeakerNames($input: ResolveSpeakerNamesInput!) {
    resolveSpeakerNames(input: $input) {
      Success
      Message
      NamesApplied
    }
  }
`;
