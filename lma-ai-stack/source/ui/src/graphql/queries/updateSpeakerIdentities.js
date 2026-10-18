/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Write the whole speaker identity map for a meeting.
 *
 * The map is sent in full rather than as a patch, so two people editing names on
 * the same meeting at once is last-write-wins.
 *
 * The selection set must be the FULL Call field set -- matching onUpdateCall's --
 * not just the field being written. This mutation is in onUpdateCall's
 * @aws_subscribe list, and AppSync publishes the mutation's own response to
 * subscribers: any field the subscription selects that the mutation did not
 * arrives as null. Since the subscription handler replaces the stored call with
 * what it receives, a two-field payload would blank out the meeting's status,
 * timestamps and summary in every connected client's meeting list. Every other
 * Call mutation in this repo enumerates the full set for the same reason.
 */
export default /* GraphQL */ `
  mutation UpdateSpeakerIdentities($input: UpdateSpeakerIdentitiesInput!) {
    updateSpeakerIdentities(input: $input) {
      PK
      SK
      CallId
      AgentId
      CallCategories
      IssuesDetected
      CallSummaryText
      SpeakerIdentities
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
