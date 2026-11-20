/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The summary profiles a meeting can be summarised with, default first: each
 * one's name, label, description and whether it lists structured action items.
 * Read by every signed-in user.
 */
export default /* GraphQL */ `
  query ListSummaryProfiles {
    listSummaryProfiles {
      Name
      Label
      Description
      StructuredActionItems
    }
  }
`;
