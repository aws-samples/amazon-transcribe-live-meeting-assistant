/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import { describe, it, expect } from 'vitest';

import { formatDuration, formatOffset, mergeSegment, mergeSegments } from './participant-transcript';

const line = (id, start, extra = {}) => ({
  SegmentId: id,
  StartTime: start,
  EndTime: start + 1,
  Channel: 'REMOTE',
  Speaker: 'Other Participant (spk_0)',
  Transcript: `line ${id}`,
  IsPartial: false,
  ...extra,
});

describe('the Participant transcript', () => {
  it('orders lines by when they were spoken and replaces a line by its id', () => {
    const lines = mergeSegments([], [line('b', 5), line('a', 1, { IsPartial: true })]);
    expect(lines.map((l) => l.SegmentId)).toEqual(['a', 'b']);
    const final = mergeSegment(lines, line('a', 1, { Transcript: 'final' }));
    expect(final.map((l) => [l.SegmentId, l.Transcript])).toEqual([
      ['a', 'final'],
      ['b', 'line b'],
    ]);
  });

  it('never replaces a final line with a late partial one', () => {
    const lines = mergeSegments([], [line('a', 1)]);
    expect(mergeSegment(lines, line('a', 1, { IsPartial: true, Transcript: 'late' }))[0].Transcript).toBe('line a');
  });

  it('keeps only what was said and typed in the meeting chat', () => {
    const lines = mergeSegments(
      [],
      [
        line('a', 1),
        line('c', 2, { Channel: 'MEETING_CHAT', Speaker: 'Olga' }),
        line('x', 3, { Channel: 'CHAT_ASSISTANT' }),
        line('y', 4, { Channel: 'AGENT_ASSISTANT' }),
      ],
    );
    expect(lines.map((l) => l.SegmentId)).toEqual(['a', 'c']);
  });

  it('formats times from the start of the meeting', () => {
    expect(formatOffset(3725)).toBe('1:02:05');
    expect(formatOffset(undefined)).toBe('0:00');
    expect(formatDuration(90500)).toBe('1:30');
    expect(formatDuration(0)).toBe('');
  });
});
