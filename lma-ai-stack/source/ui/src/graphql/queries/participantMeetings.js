/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * A Participant's read-only view of the meetings shared with them. Each
 * operation is admitted to the Participant role only, and answers a meeting
 * only when its sharing list names the caller. See
 * docs/user-based-access-control.md#participant.
 */

export const listParticipantMeetings = /* GraphQL */ `
  query ListParticipantMeetings($limit: Int, $nextToken: String) {
    listParticipantMeetings(limit: $limit, nextToken: $nextToken) {
      Meetings {
        CallId
        CreatedAt
        UpdatedAt
        Status
        TotalConversationDurationMillis
        Owner
        MeetingPlatform
      }
      nextToken
    }
  }
`;

export const getParticipantMeeting = /* GraphQL */ `
  query GetParticipantMeeting($CallId: ID!) {
    getParticipantMeeting(CallId: $CallId) {
      CallId
      CreatedAt
      UpdatedAt
      Status
      TotalConversationDurationMillis
      Owner
      MeetingPlatform
      Agenda
      CallSummaryText
      SummaryStatus
      ActionItems {
        Text
        Owner
        DueDate
        DueText
        StartTime
        SegmentId
      }
      SpeakerIdentities
      OtherParticipantsName
      ParticipantNames
      Consent {
        Mode
        Rule
        Status
        CaptureAllowed
        NoticeText
        NoticePostedAt
        UpdatedAt
        Replies {
          ParticipantName
          Decision
          Source
          OccurredAt
        }
      }
      HasAudioRecording
      HasVideoRecording
      Sessions {
        SessionNumber
        OffsetSeconds
        StartedAt
      }
      TranscriptDeletedAt
      RecordingsDeletedAt
    }
  }
`;

export const getParticipantMeetingTranscript = /* GraphQL */ `
  query GetParticipantMeetingTranscript($CallId: ID!, $nextToken: String) {
    getParticipantMeetingTranscript(CallId: $CallId, nextToken: $nextToken) {
      Segments {
        CallId
        SegmentId
        StartTime
        EndTime
        Transcript
        IsPartial
        Channel
        Speaker
        LanguageCode
        CreatedAt
      }
      nextToken
    }
  }
`;

export const getParticipantMeetingRecordings = /* GraphQL */ `
  query GetParticipantMeetingRecordings($CallId: ID!) {
    getParticipantMeetingRecordings(CallId: $CallId) {
      Kind
      SessionNumber
      Url
      ExpiresInSeconds
    }
  }
`;

// Only the fields a ParticipantTranscriptSegment has: the subscription's
// resolver refuses a selection of any other (participantTranscriptSubscription.js).
export const onAddParticipantTranscriptSegment = /* GraphQL */ `
  subscription OnAddParticipantTranscriptSegment($CallId: ID!) {
    onAddParticipantTranscriptSegment(CallId: $CallId) {
      CallId
      SegmentId
      StartTime
      EndTime
      Transcript
      IsPartial
      Channel
      Speaker
      LanguageCode
      CreatedAt
    }
  }
`;
