/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

export default /* GraphQL */ `
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
