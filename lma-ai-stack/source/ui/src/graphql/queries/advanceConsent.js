/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Consent given in advance: the signed-in person's own consents, and a
 * meeting's consent page as they see it. Every role may call these; each acts
 * on the caller's own records only. See
 * docs/meeting-consent.md#consent-given-in-advance.
 */

const CONSENT_FIELDS = `
  ConsentId
  Scope
  CallId
  VirtualParticipantId
  MeetingName
  Organizer
  LabelKey
  LabelValue
  GrantedAt
  ValidUntil
  WithdrawnAt
  WithdrawnReason
  NoticeText
  NoticeHash
  NoticeVersion
  Source
  Email
  Status
  DeviceDetailsKept
`;

export const listMyAdvanceConsents = /* GraphQL */ `
  query ListMyAdvanceConsents {
    listMyAdvanceConsents {
      Enabled
      MaxDays
      Email
      Consents {
        ${CONSENT_FIELDS}
      }
    }
  }
`;

export const getAdvanceConsentInvitation = /* GraphQL */ `
  query GetAdvanceConsentInvitation(
    $CallId: ID
    $VirtualParticipantId: ID
    $Scope: String
    $ValidUntil: String
    $LabelKey: String
    $Organizer: String
    $LabelValue: String
  ) {
    getAdvanceConsentInvitation(
      CallId: $CallId
      VirtualParticipantId: $VirtualParticipantId
      Scope: $Scope
      ValidUntil: $ValidUntil
      LabelKey: $LabelKey
      Organizer: $Organizer
      LabelValue: $LabelValue
    ) {
      Listed
      Requested
      Enabled
      Email
      ListedName
      CallId
      VirtualParticipantId
      MeetingName
      Organizer
      ScheduledFor
      Labels {
        Key
        Value
      }
      Scope
      ValidUntil
      LabelKey
      LabelValue
      NoticeText
      NoticeHash
      MaxDays
      ActiveConsents {
        ${CONSENT_FIELDS}
      }
    }
  }
`;

export const giveAdvanceConsent = /* GraphQL */ `
  mutation GiveAdvanceConsent($input: GiveAdvanceConsentInput!) {
    giveAdvanceConsent(input: $input) {
      ${CONSENT_FIELDS}
    }
  }
`;

export const withdrawAdvanceConsent = /* GraphQL */ `
  mutation WithdrawAdvanceConsent($input: WithdrawAdvanceConsentInput!) {
    withdrawAdvanceConsent(input: $input) {
      ${CONSENT_FIELDS}
    }
  }
`;

export const askToBeAddedToMeeting = /* GraphQL */ `
  mutation AskToBeAddedToMeeting($input: AskToBeAddedToMeetingInput!) {
    askToBeAddedToMeeting(input: $input) {
      Requested
      Listed
    }
  }
`;
