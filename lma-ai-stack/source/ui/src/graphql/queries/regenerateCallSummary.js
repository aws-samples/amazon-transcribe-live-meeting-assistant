/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Ask the backend to summarise the meeting again, now.
 *
 * The summary is not in this response. Summarising calls a model once per
 * prompt template and routinely takes longer than the 30 seconds AppSync allows
 * a resolver, so the backend starts a run and returns; the new text arrives
 * through onUpdateCall, by the same route an end-of-meeting summary does. What
 * comes back is only whether a run was accepted, and if not, why.
 */
export default /* GraphQL */ `
  mutation RegenerateCallSummary($input: RegenerateCallSummaryInput!) {
    regenerateCallSummary(input: $input) {
      Success
      Message
    }
  }
`;
