/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Store a meeting's agenda.
 *
 * The selection set is the FULL Call field set, matching onUpdateCall's, for the
 * reason given in updateSpeakerIdentities.js: this mutation is published to
 * onUpdateCall, and a field the subscription selects but the mutation did not
 * arrives as null in every connected client.
 */
export default /* GraphQL */ `
  mutation UpdateCallAgenda($input: UpdateCallAgendaInput!) {
    updateCallAgenda(input: $input) {
      PK
      SK
      CallId
      AgentId
      CallCategories
      IssuesDetected
      CallSummaryText
      CallSummaryState
      SpeakerIdentities
      Agenda
      AgendaSource
      AgendaUpdatedBy
      AgendaUpdatedAt
      Status
      UpdatedAt
      CreatedAt
      CustomerPhoneNumber
      SystemPhoneNumber
      RecordingUrl
      VideoRecordingUrl
      PcaUrl
      Owner
      SharedWith
      TotalConversationDurationMillis
      Sentiment {
        OverallSentiment {
          AGENT
          CALLER
        }
        SentimentByPeriod {
          QUARTER {
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
