/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The meeting owner's attestation that everyone agreed.
 *
 * Selects the whole consent record: the answer is published to
 * onUpdateMeetingConsent, and a subscriber receives only the fields the writer
 * selected (docs/meeting-consent.md). The meeting page reads getMeetingConsent
 * again afterwards for the evidence links, which only that query signs.
 */
export default /* GraphQL */ `
  mutation AttestMeetingConsent($input: AttestMeetingConsentInput!) {
    attestMeetingConsent(input: $input) {
      CallId
      Owner
      SharedWith
      Consent {
        Mode
        Rule
        Status
        CaptureAllowed
        NoticeText
        NoticePostedAt
        Notices {
          Channel
          Text
          PostedAt
          PostedBy
        }
        Participants
        ParticipantsSeen
        Events {
          EventId
          ParticipantName
          Decision
          Source
          Text
          OccurredAt
          RecordedAt
          RecordedBy
        }
        Attestation {
          AttestedBy
          Method
          Statement
          AttestedAt
          RecordedBy
        }
        Evidence {
          EvidenceId
          Kind
          S3Key
          CapturedAt
          AddedAt
          Note
          SignedUrl
          ImageDeletedAt
          Verification {
            Verdict
            MissingParticipants
            Readable
            NoticeVisible
            NoticeMatches
            Replies {
              Name
              Text
              Decision
            }
            AnyObjection
            Confidence
            Summary
            ModelId
            VerifiedAt
            VerifiedBy
            InputTokens
            OutputTokens
          }
        }
        StatusHistory {
          Status
          CaptureAllowed
          At
          Reason
        }
        StartedAt
        StartedBy
        UpdatedAt
        Version
      }
    }
  }
`;
