/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Store a meeting's summary text, the state object beside it, or both.
 *
 * Both fields are optional server-side so the same mutation can record a
 * regeneration starting or failing without touching the text. From the UI only
 * the hand-edit path uses it, which always sends both.
 *
 * The selection set must be the FULL Call field set -- matching onUpdateCall's --
 * not just the fields being written. This mutation is in onUpdateCall's
 * @aws_subscribe list, and AppSync publishes the mutation's own response to
 * subscribers: any field the subscription selects that the mutation did not
 * arrives as null. Since the subscription handler replaces the stored call with
 * what it receives, a two-field payload would blank out the meeting's status,
 * timestamps and speaker names in every connected client's meeting list. Every
 * other browser-reachable Call mutation enumerates the full set for the same
 * reason. The pipeline's own Call mutations select a fixed list that leaves
 * CallSummaryState out, deliberately -- see setCallsDeduped, which carries the
 * stored value over when one of those arrives.
 */
export default /* GraphQL */ `
  mutation UpdateCallSummaryText($input: UpdateCallSummaryTextInput!) {
    updateCallSummaryText(input: $input) {
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
