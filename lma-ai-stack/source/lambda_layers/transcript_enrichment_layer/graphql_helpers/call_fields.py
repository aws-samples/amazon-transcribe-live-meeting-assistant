# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""Call type field selector"""

from typing import Tuple

from gql.dsl import DSLField, DSLSchema

# The meeting sentiment's per-channel fields. REMOTE and LOCAL are written; AGENT
# and CALLER hold an older meeting's values and are still read. Kept identical to
# the selection of onUpdateCall in the UI, which AppSync fills from this
# mutation's response.
CHANNELS = ("REMOTE", "LOCAL", "AGENT", "CALLER")


def _summary_state(schema: DSLSchema) -> Tuple[DSLField, ...]:
    """Call.CallSummaryState, when the schema the API is running declares it.

    Selected so that the two writes which carry it -- the ENDED status and the
    end-of-meeting summary -- publish it to onUpdateCall, and every other write
    publishes the stored value rather than a null the browser has to paper over.

    Conditional because the schema a selector is built against is the one the
    API is running, which during a stack update can be older than this layer:
    naming a field it does not declare would fail every write the pipeline
    makes. Without the field the browser carries the stored value over, as it
    did before this was selected.
    """
    # pylint: disable-next=protected-access
    if "CallSummaryState" in schema.Call._type.fields:
        return (schema.Call.CallSummaryState,)
    return ()


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
        schema.Call.CallSummaryText,
        *_summary_state(schema),
        schema.Call.CustomerPhoneNumber,
        schema.Call.SystemPhoneNumber,
        schema.Call.RecordingUrl,
        schema.Call.VideoRecordingUrl,
        schema.Call.Owner,
        schema.Call.SharedWith,
        schema.Call.TotalConversationDurationMillis,
        schema.Call.Sentiment.select(
            overall_sentiment_select,
            sentiment_type_period_select,
        ),
    )
