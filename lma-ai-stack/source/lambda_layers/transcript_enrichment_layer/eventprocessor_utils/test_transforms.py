# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Tests for the sentiment transform in eventprocessor_utils.

`transform_segment_to_add_sentiment` scores a final transcript segment with
Amazon Comprehend and reports each Comprehend request to a metering hook. These
tests pin that the hook is called once per request, that a failing hook does not
cost the segment its sentiment, and that a segment that already carries a label
is not sent to Comprehend at all.

No AWS: the Comprehend client is a stub.
"""

from __future__ import annotations

import eventprocessor_utils.eventprocessor as ep


class _Comprehend:
    def detect_sentiment(self, **_kwargs):
        return {"Sentiment": "NEUTRAL", "SentimentScore": {"Neutral": 1.0}}


def test_each_comprehend_request_is_reported_for_metering() -> None:
    import asyncio

    seen = []
    args = {
        "comprehend_client": _Comprehend(),
        "on_comprehend_request": lambda call_id, text: seen.append((call_id, text)),
    }
    message = {"CallId": "m-1", "Transcript": "we ship on friday"}
    result = asyncio.run(ep.transform_segment_to_add_sentiment(message, args))
    assert result["Sentiment"] == "NEUTRAL"
    assert seen == [("m-1", "we ship on friday")]


def test_a_failing_metering_hook_does_not_cost_the_segment_its_sentiment() -> None:
    import asyncio

    def broken(_call_id, _text):
        raise RuntimeError("table unavailable")

    args = {"comprehend_client": _Comprehend(), "on_comprehend_request": broken}
    result = asyncio.run(
        ep.transform_segment_to_add_sentiment({"CallId": "m-1", "Transcript": "x"}, args)
    )
    assert result["Sentiment"] == "NEUTRAL"


def test_a_supplied_sentiment_label_is_not_metered() -> None:
    import asyncio

    seen = []
    args = {"on_comprehend_request": lambda *a: seen.append(a)}
    asyncio.run(
        ep.transform_segment_to_add_sentiment(
            {"CallId": "m-1", "Transcript": "x", "Sentiment": "POSITIVE"}, args
        )
    )
    assert seen == []
