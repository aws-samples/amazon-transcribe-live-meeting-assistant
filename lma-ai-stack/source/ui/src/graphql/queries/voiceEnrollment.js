/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * What the portal shows its visitor about their own enrollment.
 *
 * Includes the terms text, because the hash of exactly what was displayed is
 * stored with the consent -- so the text has to come from the backend that will
 * hash it, not from a copy in this bundle that could drift from it.
 */
export const voiceEnrollmentTerms = /* GraphQL */ `
  query VoiceEnrollmentTerms {
    voiceEnrollmentTerms {
      Success
      Message
      Status
      DisplayName
      ConsentExpiresAt
      NeedsReconsent
      TermsVersion
      Terms
    }
  }
`;

/** Record that the visitor themselves agreed to the current terms. */
export const grantVoiceConsent = /* GraphQL */ `
  mutation GrantVoiceConsent {
    grantVoiceConsent {
      Success
      Message
      Status
    }
  }
`;

/**
 * Send the recordings and store the voiceprint.
 *
 * The recordings are used to compute it and then discarded -- they are never
 * written to S3 or a table, which is what the terms promise.
 */
export const enrolMyVoice = /* GraphQL */ `
  mutation EnrolMyVoice($input: EnrolMyVoiceInput!) {
    enrolMyVoice(input: $input) {
      Success
      Message
      Status
    }
  }
`;

/** Withdraw, which deletes the voiceprint immediately. */
export const revokeVoiceConsent = /* GraphQL */ `
  mutation RevokeVoiceConsent($input: RevokeVoiceConsentInput) {
    revokeVoiceConsent(input: $input) {
      Success
      Message
      Status
    }
  }
`;
