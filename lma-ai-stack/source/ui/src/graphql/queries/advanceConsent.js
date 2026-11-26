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
  WithdrawnNote
  NoticeText
  NoticeHash
  NoticeVersion
  Source
  Email
  Status
  DeviceDetailsKept
`;

const NAMES_FIELDS = `
  DisplayName
  ShortNames
  AccountName
  UpdatedAt
`;

/**
 * For Admins: every consent given in advance, newest first, a page at a time,
 * with the account's display name and the meetings that counted each one; a
 * withdrawal on a person's behalf; and the permanent removal of a person's
 * consent records. See
 * docs/meeting-consent.md#administering-consents-given-in-advance.
 */
const ADMIN_CONSENT_FIELDS = `
  ${CONSENT_FIELDS}
  WithdrawnBy
  DisplayName
  Supersedes
  CountedIn {
    CallId
    At
  }
`;

export const listAllAdvanceConsents = /* GraphQL */ `
  query ListAllAdvanceConsents(
    $Person: String
    $Scope: String
    $State: String
    $ExpiringBefore: String
    $Limit: Int
    $NextToken: String
  ) {
    listAllAdvanceConsents(
      Person: $Person
      Scope: $Scope
      State: $State
      ExpiringBefore: $ExpiringBefore
      Limit: $Limit
      NextToken: $NextToken
    ) {
      Consents {
        ${ADMIN_CONSENT_FIELDS}
      }
      NextToken
    }
  }
`;

export const withdrawAdvanceConsentAsAdmin = /* GraphQL */ `
  mutation WithdrawAdvanceConsentAsAdmin($input: WithdrawAdvanceConsentAsAdminInput!) {
    withdrawAdvanceConsentAsAdmin(input: $input) {
      ${ADMIN_CONSENT_FIELDS}
    }
  }
`;

export const deleteAdvanceConsentRecords = /* GraphQL */ `
  mutation DeleteAdvanceConsentRecords($input: DeleteAdvanceConsentRecordsInput!) {
    deleteAdvanceConsentRecords(input: $input) {
      Deleted
      Withdrawn
      ConsentIds
      AuditId
      MoreRemain
    }
  }
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
      Names {
        ${NAMES_FIELDS}
      }
    }
  }
`;

/**
 * The caller's own names in meetings: how LMA recognises them among a
 * meeting's attendees for their consent given in advance. See
 * docs/meeting-consent.md#the-match-is-by-display-name.
 */
export const updateMyAccountNames = /* GraphQL */ `
  mutation UpdateMyAccountNames($input: UpdateMyAccountNamesInput!) {
    updateMyAccountNames(input: $input) {
      ${NAMES_FIELDS}
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
