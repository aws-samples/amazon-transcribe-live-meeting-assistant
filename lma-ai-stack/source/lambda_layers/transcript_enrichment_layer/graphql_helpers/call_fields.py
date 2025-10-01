# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""Call type field selector"""

from typing import Tuple
from gql.dsl import DSLField, DSLSchema


CHANNELS = ("AGENT", "CALLER")


def call_fields(schema: DSLSchema) -> Tuple[DSLField, ...]:
    """Call type field selector"""
    overall_sentiment_select = schema.SentimentAggregation.OverallSentiment.select(
        *(getattr(schema.OverallSentiment, c) for c in CHANNELS)
    )
    sentiment_by_channel_entry_select = (
        schema.SentimentByChannelEntry.BeginOffsetMillis,
        schema.SentimentByChannelEntry.EndOffsetMillis,
        schema.SentimentByChannelEntry.Score,
    )
    sentiment_by_channel_select = (
        getattr(schema.SentimentByChannel, c).select(*sentiment_by_channel_entry_select)
        for c in CHANNELS
    )
    sentiment_type_period_select = schema.SentimentAggregation.SentimentByPeriod.select(
        schema.SentimentByPeriod.QUARTER.select(
            *sentiment_by_channel_select,
        )
    )

    return (
        schema.Call.PK,
        schema.Call.SK,
        schema.Call.CallId,
        schema.Call.Status,
        schema.Call.CreatedAt,
        schema.Call.UpdatedAt,
        schema.Call.AgentId,
        schema.Call.CallCategories,
        schema.Call.IssuesDetected,
        schema.Call.CallSummaryText,
        schema.Call.CustomerPhoneNumber,
        schema.Call.SystemPhoneNumber,
        schema.Call.RecordingUrl,
        schema.Call.PcaUrl,
        schema.Call.Owner,
        schema.Call.SharedWith,
        schema.Call.TotalConversationDurationMillis,
        schema.Call.Sentiment.select(
            overall_sentiment_select,
            sentiment_type_period_select,
        ),
    )
