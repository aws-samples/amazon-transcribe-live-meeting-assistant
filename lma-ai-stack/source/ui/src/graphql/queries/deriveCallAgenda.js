/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Read a meeting's transcript for an agenda somebody stated.
 *
 * A found agenda is not in this response: it is stored through updateCallAgenda
 * and arrives through onUpdateCall like any other agenda change.
 */
export default /* GraphQL */ `
  mutation DeriveCallAgenda($input: DeriveCallAgendaInput!) {
    deriveCallAgenda(input: $input) {
      Success
      Message
      Derived
    }
  }
`;
