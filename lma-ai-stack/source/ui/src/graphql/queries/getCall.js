/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

export default /* GraphQL */ `
  query Query($callId: ID!) {
    getCall(CallId: $callId) {
      CallId
      AgentId
      Owner
      SharedWith
      CallSummaryText
      CallSummaryState
      SpeakerIdentities
      Agenda
      AgendaSource
      AgendaUpdatedBy
      AgendaUpdatedAt
      Labels {
        Key
        Value
      }
      Audience
      MeetingMetadataUpdatedAt
      MeetingContext
      MeetingContextUpdatedBy
      MeetingContextUpdatedAt
      Checklist {
        ItemId
        Text
        Kind
        Priority
        Source
        Status
        StatusSource
        Evidence {
          SegmentId
          StartTime
          Quote
        }
        Note
        UpdatedBy
        UpdatedAt
      }
      ChecklistUpdatedAt
      SummaryProfile
      GuidanceEnabled
      ActionItems {
        Text
        Owner
        DueDate
        DueText
        StartTime
        SegmentId
      }
      MeetingPlatform
      Participants {
        name
        email
      }
      ParticipantsUpdatedBy
      ParticipantsUpdatedAt
      WhiteboardsEnabled
      TranscriptDeletionScheduledFor
      TranscriptDeletedAt
      TranscriptSegmentsDeleted
      TranscriptFilesDeleted
      RecordingDeletionScheduledFor
      RecordingsDeletedAt
      RecordingFilesDeleted
      ExpiresAfter
      CreatedAt
      CustomerPhoneNumber
      Status
      SystemPhoneNumber
      UpdatedAt
      RecordingUrl
      VideoRecordingUrl
      RecordingUrls
      RecordingUrlSessions
      VideoRecordingUrls
      VideoRecordingUrlSessions
      SessionNumber
      SessionOffsetSeconds
      SessionStartedNumber
      Sessions {
        SessionNumber
        OffsetSeconds
        StartedAt
      }
      CaptureSource
      TranscriptionEngine
      TranscriptionLanguage
      SpeakerSeparation
      DetectedLanguages
      TranscriptionPause
      TotalConversationDurationMillis
      Sentiment {
        OverallSentiment {
          REMOTE
          LOCAL
          AGENT
          CALLER
        }
        SentimentByPeriod {
          QUARTER {
            REMOTE {
              BeginOffsetMillis
              EndOffsetMillis
              Score
            }
            LOCAL {
              BeginOffsetMillis
              EndOffsetMillis
              Score
            }
            AGENT {
              BeginOffsetMillis
              EndOffsetMillis
              Score
            }
            CALLER {
              BeginOffsetMillis
              EndOffsetMillis
              Score
            }
          }
        }
      }
    }
  }
`;
