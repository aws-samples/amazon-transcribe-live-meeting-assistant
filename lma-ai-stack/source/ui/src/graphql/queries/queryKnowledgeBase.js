/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

export default /* GraphQL */ `
  query Query($input: String!, $sessionId: String) {
    queryKnowledgeBase(input: $input, sessionId: $sessionId)
  }
`;
