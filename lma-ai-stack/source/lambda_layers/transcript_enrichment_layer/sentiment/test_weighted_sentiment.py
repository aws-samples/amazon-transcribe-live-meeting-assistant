# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Tests for the weighted sentiment score shown on the meeting timeline.

`ComprehendWeightedSentiment` turns an Amazon Comprehend DetectSentiment response
into the single signed number the UI plots. It is pure arithmetic over a response
dict, so all of it is reachable without AWS — and worth pinning, because the
output is a number that looks plausible whatever the logic does. A sign flip or a
threshold applied the wrong way round produces a chart that is confidently wrong
rather than an error anyone notices.

The behaviour these fix in place:

* Only POSITIVE and NEGATIVE are scored. MIXED and NEUTRAL return None, meaning
  "no point on the chart" rather than zero, which would read as neutral sentiment
  that Comprehend never reported.
* A strong negative score overrides whatever Comprehend called the sentiment, so
  an utterance Comprehend labels POSITIVE but scores heavily negative is plotted
  negative.
* Scores below the configured threshold are discarded rather than plotted small.
"""

from __future__ import annotations

import pytest

from sentiment.weighted_sentiment import ComprehendWeightedSentiment


def response(sentiment: str, positive: float = 0.0, negative: float = 0.0) -> dict:
    """A DetectSentiment response carrying just the fields the class reads."""
    return {
        "Sentiment": sentiment,
        "SentimentScore": {
            "Positive": positive,
            "Negative": negative,
            "Neutral": 0.0,
            "Mixed": 0.0,
        },
    }


@pytest.fixture(name="scorer")
def _scorer() -> ComprehendWeightedSentiment:
    """Explicit thresholds, so the tests do not depend on the ambient defaults."""
    return ComprehendWeightedSentiment(
        scale_range=5, negative_threshold=0.4, positive_threshold=0.4
    )


# ── what is and is not scored ───────────────────────────────────────────────


@pytest.mark.parametrize("sentiment", ["MIXED", "NEUTRAL"])
def test_mixed_and_neutral_are_not_scored(scorer, sentiment):
    """None means no point plotted; 0.0 would read as measured neutrality."""
    assert scorer.get_weighted_sentiment_score(response(sentiment, 0.9, 0.05)) is None


def test_an_unrecognised_sentiment_is_not_scored(scorer):
    assert scorer.get_weighted_sentiment_score(response("SOMETHING_ELSE", 0.9)) is None


# ── the scale ───────────────────────────────────────────────────────────────


def test_a_full_confidence_positive_reaches_the_top_of_the_scale(scorer):
    assert scorer.get_weighted_sentiment_score(response("POSITIVE", positive=1.0)) == 5.0


def test_a_full_confidence_negative_reaches_the_bottom_of_the_scale(scorer):
    assert scorer.get_weighted_sentiment_score(response("NEGATIVE", negative=1.0)) == -5.0


def test_a_positive_score_is_the_confidence_times_the_scale(scorer):
    assert scorer.get_weighted_sentiment_score(
        response("POSITIVE", positive=0.6)
    ) == pytest.approx(3.0)


def test_a_negative_score_is_signed(scorer):
    """The sign is what the chart reads; losing it inverts the whole timeline."""
    score = scorer.get_weighted_sentiment_score(response("NEGATIVE", negative=0.8))
    assert score == pytest.approx(-4.0)
    assert score < 0


def test_the_scale_range_is_configurable(scorer):
    wider = ComprehendWeightedSentiment(
        scale_range=10, negative_threshold=0.4, positive_threshold=0.4
    )
    assert wider.get_weighted_sentiment_score(response("POSITIVE", positive=1.0)) == 10.0
    assert wider.get_weighted_sentiment_score(response("NEGATIVE", negative=1.0)) == -10.0


# ── thresholds ──────────────────────────────────────────────────────────────


def test_a_positive_below_the_threshold_is_discarded(scorer):
    """Plotted small would imply a measurement the threshold says to distrust."""
    assert scorer.get_weighted_sentiment_score(response("POSITIVE", positive=0.2)) is None


def test_a_positive_at_the_threshold_is_kept(scorer):
    """The boundary is inclusive; an off-by-one here silently drops a whole band."""
    assert scorer.get_weighted_sentiment_score(
        response("POSITIVE", positive=0.4)
    ) == pytest.approx(2.0)


def test_a_strong_negative_overrides_a_positive_label(scorer):
    """Comprehend's label loses to a negative score above the threshold.

    An utterance it calls POSITIVE while scoring negative heavily is plotted
    negative — the case this override exists for.
    """
    score = scorer.get_weighted_sentiment_score(
        response("POSITIVE", positive=0.45, negative=0.55)
    )
    assert score == pytest.approx(-2.75)
    assert score < 0


def test_a_weak_negative_does_not_override_a_positive_label(scorer):
    """Below the threshold the override must not fire, or every mildly mixed
    utterance would be plotted negative."""
    score = scorer.get_weighted_sentiment_score(
        response("POSITIVE", positive=0.7, negative=0.2)
    )
    assert score == pytest.approx(3.5)
    assert score > 0


def test_thresholds_are_independently_configurable(scorer):
    """A permissive positive threshold keeps a score the default would discard."""
    permissive = ComprehendWeightedSentiment(
        scale_range=5, negative_threshold=0.9, positive_threshold=0.1
    )
    assert permissive.get_weighted_sentiment_score(
        response("POSITIVE", positive=0.2)
    ) == pytest.approx(1.0)
    # and the raised negative threshold stops the override firing at 0.55
    assert permissive.get_weighted_sentiment_score(
        response("POSITIVE", positive=0.45, negative=0.55)
    ) == pytest.approx(2.25)


# ── the score read off the response ─────────────────────────────────────────


def test_the_score_is_read_from_the_matching_case_key():
    """Comprehend keys SentimentScore in title case while Sentiment is upper.

    The class title-cases the sentiment to look the score up, so a change to
    either side has to keep them in step.
    """
    scorer = ComprehendWeightedSentiment(
        scale_range=5, negative_threshold=0.4, positive_threshold=0.4
    )
    payload = response("POSITIVE", positive=0.8)
    assert set(payload["SentimentScore"]) == {"Positive", "Negative", "Neutral", "Mixed"}
    assert scorer.get_weighted_sentiment_score(payload) == pytest.approx(4.0)
