// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import gql from 'graphql-tag';

export default gql`
  subscription Subscription($callId: ID) {
    onAddTranscriptSegment(CallId: $callId) {
      PK
      SK
      CreatedAt
      CallId
      SegmentId
      StartTime
      EndTime
      Speaker
      Transcript
      IsPartial
      Channel
      Owner
      SharedWith
      Sentiment
      SentimentScore {
        Positive
        Negative
        Neutral
        Mixed
      }
      SentimentWeighted
    }
  }
`;
