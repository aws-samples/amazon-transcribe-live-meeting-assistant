/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
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
