/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Replace a meeting's participant list (its owner or an Admin only).
 *
 * Not published to onUpdateCall: only the page that saves it, and getCall,
 * read the list. The selection is updateCallLabels.js's Call field set plus the
 * participant list, so the meeting's state can take the answer whole (see
 * mergeCallUpdate). See docs/virtual-participant.md#participant-list.
 */
export default /* GraphQL */ `
  mutation UpdateCallParticipants($input: UpdateCallParticipantsInput!) {
    updateCallParticipants(input: $input) {
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
