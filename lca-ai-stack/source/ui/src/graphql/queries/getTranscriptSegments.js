// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import gql from 'graphql-tag';

export default gql`
  query Query($callId: ID!, $isPartial: Boolean) {
    getTranscriptSegments(callId: $callId, isPartial: $isPartial) {
      TranscriptSegments {
        Channel
        CallId
        CreatedAt
        EndTime
        IsPartial
        PK
        SK
        SegmentId
        StartTime
        Speaker
        Transcript
        Sentiment
        SentimentScore {
          Positive
          Negative
          Neutral
          Mixed
        }
        SentimentWeighted
      }
      nextToken
    }
  }
`;
