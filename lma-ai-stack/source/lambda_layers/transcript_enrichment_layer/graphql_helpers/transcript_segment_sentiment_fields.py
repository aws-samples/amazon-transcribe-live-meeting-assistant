# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""Transcript Segment type field selector"""

from typing import Tuple
from gql.dsl import DSLField, DSLSchema


def transcript_segment_sentiment_fields(schema: DSLSchema) -> Tuple[DSLField, ...]:
    """Transcript Segment Sentiment type field selector"""
    return (
        schema.TranscriptSegment.Sentiment,
        schema.TranscriptSegment.SentimentWeighted,
        schema.TranscriptSegment.SentimentScore.select(
            schema.SentimentScore.Positive,
            schema.SentimentScore.Negative,
            schema.SentimentScore.Neutral,
            schema.SentimentScore.Mixed,
        ),
    )
