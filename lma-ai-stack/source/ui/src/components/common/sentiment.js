/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
export const getWeightedSentimentLabel = (sentimentWeighted) => {
  if (sentimentWeighted > 0) {
    return 'POSITIVE';
  }
  if (sentimentWeighted < 0) {
    return 'NEGATIVE';
  }
  return 'NEUTRAL';
};

export const getSentimentTrendLabel = (sentimentByQuarter) => {
  if (sentimentByQuarter.length <= 1) {
    return 'FLAT';
  }
  const sentimentByQuarterValues = sentimentByQuarter.filter((s) => s.EndOffsetMillis > 0).map((s) => s.Score || 0);

  const lastQuarterValue = sentimentByQuarterValues.slice(-1);
  const previousQuarters = sentimentByQuarterValues.slice(0, -1);
  if (previousQuarters.length < 1) {
    return 'FLAT';
  }
  const previousQuarterAverage = previousQuarters.reduce((p, c) => p + c) / previousQuarters.length;
  if (previousQuarterAverage > lastQuarterValue) {
    return 'DOWN';
  }
  if (previousQuarterAverage < lastQuarterValue) {
    return 'UP';
  }

  return 'FLAT';
};
