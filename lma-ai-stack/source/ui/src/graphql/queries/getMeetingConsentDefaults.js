/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The deployment's meeting consent mode and rule, and whether a meeting may
 * choose a less strict one. Read by every signed-in user.
 */
export default /* GraphQL */ `
  query GetMeetingConsentDefaults {
    getMeetingConsentDefaults {
      Mode
      Rule
      AllowPerMeetingOverride
    }
  }
`;
