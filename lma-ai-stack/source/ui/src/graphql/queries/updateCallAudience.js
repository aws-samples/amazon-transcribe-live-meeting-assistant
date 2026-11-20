/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Set or clear a meeting's audience.
 *
 * The selection set is the FULL Call field set, matching onUpdateCall's, for the
 * reason given in updateSpeakerIdentities.js: this mutation is published to
 * onUpdateCall, and a field the subscription selects but the mutation did not
 * arrives as null in every connected client. See
 * docs/meeting-context-and-checklist.md.
 */
export default /* GraphQL */ `
  mutation UpdateCallAudience($input: UpdateCallAudienceInput!) {
    updateCallAudience(input: $input) {
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
