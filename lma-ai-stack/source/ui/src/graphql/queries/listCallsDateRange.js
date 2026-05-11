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
        CreatedAt
        CustomerPhoneNumber
        Status
        SystemPhoneNumber
        UpdatedAt
        RecordingUrl
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
