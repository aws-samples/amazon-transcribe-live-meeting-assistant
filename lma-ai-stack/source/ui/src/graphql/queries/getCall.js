// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import gql from 'graphql-tag';

export default gql`
  query Query($callId: ID!) {
    getCall(CallId: $callId) {
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
  }
`;
