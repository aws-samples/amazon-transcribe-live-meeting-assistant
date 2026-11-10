/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * A meeting's consent record, with hour-long signed links to its evidence
 * images, whether it may be recorded and transcribed now, and the deployment's
 * consent settings. Answered to the callers getCall answers.
 */
export default /* GraphQL */ `
  query GetMeetingConsent($callId: ID!) {
    getMeetingConsent(CallId: $callId) {
      CallId
      CaptureAllowed
      Settings {
        Mode
        Rule
        AllowPerMeetingOverride
        KeepEvidenceScreenshots
        VerifierModelId
      }
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
