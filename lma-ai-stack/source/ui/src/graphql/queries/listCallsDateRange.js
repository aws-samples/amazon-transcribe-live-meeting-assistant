/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

export default /* GraphQL */ `
  query ListCallsDateRange($startDateTime: AWSDateTime!, $endDateTime: AWSDateTime!, $limit: Int, $nextToken: String) {
    listCallsDateRange(startDateTime: $startDateTime, endDateTime: $endDateTime, limit: $limit, nextToken: $nextToken) {
      Calls {
        PK
        SK
        CallId
        AgentId
        Owner
        SharedWith
        CallCategories
        IssuesDetected
        CallSummaryText
        # Selected here as well as in getCall, for the same reason
        # SpeakerIdentities is below: the meetings-list split panel renders the
        # meeting detail from a list-derived object, which exists before
        # setCallsDeduped has any stored value to carry over. Without this the
        # panel would read a hand-edited summary as generated and show no
        # regeneration in flight.
        CallSummaryState
        # Selected here as well as in getCall, even though only the open meeting
        # needs it. The meetings-list split panel renders the full meeting
        # detail from a list-derived object, and a rename made there with this
        # field absent writes a map with every other name, merge, override and
        # candidate missing -- destroying them. A little extra payload per
        # meeting is much cheaper than that.
        SpeakerIdentities
        CreatedAt
        CustomerPhoneNumber
        Status
        SystemPhoneNumber
        UpdatedAt
        RecordingUrl
        VideoRecordingUrl
        PcaUrl
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
      nextToken
    }
  }
`;
