/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
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
      TranscriptDeletionScheduledFor
      TranscriptDeletedAt
      TranscriptSegmentsDeleted
      TranscriptFilesDeleted
      ExpiresAfter
      CreatedAt
      CustomerPhoneNumber
      Status
      SystemPhoneNumber
      UpdatedAt
      RecordingUrl
      VideoRecordingUrl
      CaptureSource
      TranscriptionEngine
      TranscriptionLanguage
      SpeakerSeparation
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
