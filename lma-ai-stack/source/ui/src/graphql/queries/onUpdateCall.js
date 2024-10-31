// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import gql from 'graphql-tag';

export default gql`
  subscription Subscription {
    onUpdateCall {
      PK
      SK
      CallId
      AgentId
      CallCategories
      IssuesDetected
      CallSummaryText
      Status
      UpdatedAt
      CreatedAt
      CustomerPhoneNumber
      SystemPhoneNumber
      RecordingUrl
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
