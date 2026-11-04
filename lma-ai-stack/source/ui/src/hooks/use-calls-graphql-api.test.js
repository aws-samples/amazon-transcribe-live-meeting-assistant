/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import { describe, it, expect, vi } from 'vitest';

vi.mock('aws-amplify/api', () => ({ generateClient: () => ({ graphql: vi.fn() }) }));

const { fetchAllTranscriptSegments, isRequestTimeout, mapTranscriptSegmentValue } = await import(
  './use-calls-graphql-api'
);

describe('isRequestTimeout', () => {
  it("recognises AppSync's execution timeout", () => {
    expect(isRequestTimeout({ errors: [{ errorType: 'ExecutionTimeout', message: 'Execution timed out.' }] })).toBe(
      true,
    );
    expect(isRequestTimeout({ errors: [{ message: 'Execution timed out.' }] })).toBe(true);
  });

  it('does not mistake a refusal or a network failure for one', () => {
    expect(isRequestTimeout({ errors: [{ errorType: 'Unauthorized', message: 'Not Authorized' }] })).toBe(false);
    expect(isRequestTimeout(new TypeError('Failed to fetch'))).toBe(false);
    expect(isRequestTimeout(undefined)).toBe(false);
  });
});

describe('mapTranscriptSegmentValue', () => {
  it('holds a segment stored under an older channel name on the current one', () => {
    expect(mapTranscriptSegmentValue({ SegmentId: 's1', Channel: 'CALLER' }).channel).toBe('REMOTE');
    expect(mapTranscriptSegmentValue({ SegmentId: 's2', Channel: 'AGENT' }).channel).toBe('LOCAL');
  });

  it('keeps the current names and the assistant channels as they are', () => {
    ['REMOTE', 'LOCAL', 'AGENT_ASSISTANT', 'CHAT_ASSISTANT'].forEach((channel) =>
      expect(mapTranscriptSegmentValue({ SegmentId: 's', Channel: channel }).channel).toBe(channel),
    );
  });
});

describe('fetchAllTranscriptSegments', () => {
  it('reads every page, including an empty one that carries a nextToken', async () => {
    const pages = {
      null: { TranscriptSegments: [{ SegmentId: 'a' }], nextToken: 't1' },
      t1: { TranscriptSegments: [], nextToken: 't2' },
      t2: { TranscriptSegments: [{ SegmentId: 'b' }, null], nextToken: null },
    };
    const graphql = vi.fn(async ({ variables }) => ({ data: { getTranscriptSegments: pages[variables.nextToken] } }));

    const segments = await fetchAllTranscriptSegments(graphql, 'c1');

    expect(segments.map((s) => s.SegmentId)).toEqual(['a', 'b']);
    expect(graphql.mock.calls.map(([request]) => request.variables)).toEqual([
      { callId: 'c1', nextToken: null },
      { callId: 'c1', nextToken: 't1' },
      { callId: 'c1', nextToken: 't2' },
    ]);
  });
});
