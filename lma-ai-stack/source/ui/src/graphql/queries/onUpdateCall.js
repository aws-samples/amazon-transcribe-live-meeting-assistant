/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

export default /* GraphQL */ `
  subscription Subscription {
    onUpdateCall {
      PK
      SK
      CallId
      AgentId
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
      Status
      UpdatedAt
      CreatedAt
      CustomerPhoneNumber
      SystemPhoneNumber
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
      Owner
      SharedWith
      TotalConversationDurationMillis
      DetectedLanguages
      TranscriptionPause
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
