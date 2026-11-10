/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import { describe, it, expect } from 'vitest';
import mapCallsAttributes from './map-call-attributes';

const call = (sentiment) => ({ CallId: 'c1', CreatedAt: '2026-01-01T00:00:00Z', Sentiment: sentiment });
const quarters = (score) => [1, 2, 3, 4].map((i) => ({ BeginOffsetMillis: i, EndOffsetMillis: i + 1, Score: score }));

describe('mapCallsAttributes sentiment', () => {
  it('reads the REMOTE and LOCAL sentiment a meeting is written with', () => {
    const [item] = mapCallsAttributes([
      call({
        OverallSentiment: { REMOTE: 2, LOCAL: -3, CALLER: null, AGENT: null },
        SentimentByPeriod: { QUARTER: { REMOTE: quarters(2), LOCAL: quarters(-3), CALLER: null, AGENT: null } },
      }),
    ]);
    expect(item.callerAverageSentiment).toBe(2);
    expect(item.agentAverageSentiment).toBe(-3);
    expect(item.sentiment.SentimentByPeriod.QUARTER.REMOTE).toEqual(quarters(2));
  });

  it('reads a meeting stored before 0.4.0 with CALLER and AGENT sentiment as REMOTE and LOCAL', () => {
    const [item] = mapCallsAttributes([
      call({
        OverallSentiment: { REMOTE: null, LOCAL: null, CALLER: 1.5, AGENT: 0.5 },
        SentimentByPeriod: { QUARTER: { REMOTE: null, LOCAL: null, CALLER: quarters(1.5), AGENT: quarters(0.5) } },
      }),
    ]);
    expect(item.callerAverageSentiment).toBe(1.5);
    expect(item.agentAverageSentiment).toBe(0.5);
    // The charts index the quarters by the transcript's channel, which is REMOTE/LOCAL.
    expect(item.sentiment.SentimentByPeriod.QUARTER.REMOTE).toEqual(quarters(1.5));
    expect(item.sentiment.SentimentByPeriod.QUARTER.LOCAL).toEqual(quarters(0.5));
  });

  it('maps a meeting with no sentiment', () => {
    const [item] = mapCallsAttributes([call(undefined)]);
    expect(item.callerAverageSentiment).toBe(0);
    expect(item.agentAverageSentiment).toBe(0);
  });
});
