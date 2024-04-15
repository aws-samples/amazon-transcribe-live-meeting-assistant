# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Weighted Comprehend Sentiment"""
from typing import TYPE_CHECKING, Optional
from os import environ


if TYPE_CHECKING:
    from mypy_boto3_comprehend.type_defs import DetectSentimentResponseTypeDef
    from mypy_boto3_comprehend.literals import SentimentTypeType
else:
    DetectSentimentResponseTypeDef = object
    SentimentTypeType = object


class ComprehendWeightedSentiment:
    # pylint: disable=too-few-public-methods
    """Comprehend Weighted Sentiment

    Used to produce Comprehend sentiment scores based on a weighted scaled range
    """
    DEFAULT_SCALE_RANGE = 5
    DEFAULT_NEGATIVE_THRESHOLD = environ.get('SENTIMENT_NEGATIVE_THRESHOLD') or 0.4
    DEFAULT_POSITIVE_THRESHOLD = environ.get('SENTIMENT_POSITIVE_THRESHOLD') or 0.4

    def __init__(
        self,
        scale_range: int = DEFAULT_SCALE_RANGE,
        negative_threshold: float = float(DEFAULT_NEGATIVE_THRESHOLD),
        positive_threshold: float = float(DEFAULT_POSITIVE_THRESHOLD)
    ) -> None:
        """Initializes the Comprehend Weighted Sentiment

        :parameter scale_range: sentiment ranges from +/- this value
        :parameter negative_threshold: negative sentiment scores above this
        value supersedes the sentiment.
        :parameter positive_threshold: positive sentiment scores below this
        value are discarded.
        """
        self.scale_range = scale_range
        self.negative_threshold = negative_threshold
        self.positive_threshold = positive_threshold

        self.range_base = {
            "POSITIVE": scale_range,
            "NEGATIVE": -scale_range,
        }

    @staticmethod
    def _get_score_from_response(
        sentiment_response: DetectSentimentResponseTypeDef,
        sentiment: SentimentTypeType,
    ) -> float:
        sentiment_title_case = sentiment.title()
        return sentiment_response["SentimentScore"][sentiment_title_case]  # type: ignore

    def get_weighted_sentiment_score(
        self,
        sentiment_response: DetectSentimentResponseTypeDef,
    ) -> Optional[float]:
        """Get a weighted sentiment score

        :parameter sentiment_response: response from the Comprehend Detect
        sentiment API.

        Takes the response of the Comprehend Detect Sentiment API to return
        a weighted sentiment score. Uses the same algorithm as the Post Call
        Analytics solution.

        It only handles POSITIVE and NEGATIVE scores. Returns None for MIXED or
        NEUTRAL sentiment. The value is also discarded (returns None) if the
        score is below the initialized positive_threshold or negative_threshold
        values.

        The score returned from the Comprehend Detect Sentiment API is used as
        a factor and then shifted to the initialized range based on the
        initialized scale_range (range -5 to +5 by default).

        Returns a weighted score for Positive and Negative sentiments by using
        the Comprehend sentiment score as a factor. By default, Positive ranges
        from 0 to 5 and Negative from 0 to -5.

        If the Negative sentiment score returned from Comprehend is greater than
        the initialized negative_threshold, the returned score will be
        overridden and the negative value will be used instead.
        """
        sentiment = sentiment_response["Sentiment"]
        # discard if not positive or negative
        if sentiment not in self.range_base:
            return None

        sentiment_positive = self._get_score_from_response(
            sentiment_response=sentiment_response,
            sentiment="POSITIVE",
        )
        sentiment_negative = self._get_score_from_response(
            sentiment_response=sentiment_response,
            sentiment="NEGATIVE",
        )
        # override with negative sentiment if above the initialized threshold
        sentiment_key = "NEGATIVE" if sentiment_negative > self.negative_threshold else sentiment

        # discard if not above the initialized threshold
        if (sentiment_key == "NEGATIVE" and sentiment_negative < self.negative_threshold) or (
            sentiment_key == "POSITIVE" and sentiment_positive < self.positive_threshold
        ):
            return None

        sentiment_score = sentiment_negative if sentiment_key == "NEGATIVE" else sentiment_positive
        sentiment_base_value = self.range_base[sentiment_key]

        return sentiment_base_value * sentiment_score
