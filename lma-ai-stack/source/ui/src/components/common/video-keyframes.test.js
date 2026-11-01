/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import { describe, it, expect } from 'vitest';

import { formatFrameTime, mergeKeyframes, recordingSecondsFor, segmentIndexForTime } from './video-keyframes';

const frame = (ms, extra = {}) => ({
  FrameId: `${String(ms).padStart(10, '0')}-ab12cd`,
  Timestamp: ms / 1000,
  ...extra,
});

describe('mergeKeyframes', () => {
  it('keeps one frame per id, in time order, with the newer copy winning', () => {
    const merged = mergeKeyframes(
      [frame(30000, { ThumbnailSignedUrl: 'old' }), frame(5000)],
      [frame(100000), frame(30000, { ThumbnailSignedUrl: 'new' })],
    );
    expect(merged.map((f) => f.Timestamp)).toEqual([5, 30, 100]);
    expect(merged[1].ThumbnailSignedUrl).toBe('new');
  });

  it('copes with missing lists and frames without an id', () => {
    expect(mergeKeyframes(undefined, [null, {}, frame(1000)])).toHaveLength(1);
  });
});

describe('formatFrameTime', () => {
  it('shows minutes under an hour and hours from an hour', () => {
    expect(formatFrameTime(0)).toBe('0:00');
    expect(formatFrameTime(754.9)).toBe('12:34');
    expect(formatFrameTime(3725)).toBe('1:02:05');
    expect(formatFrameTime(undefined)).toBe('0:00');
  });
});

describe('recordingSecondsFor', () => {
  it('prefers the position in the recording, and falls back to the transcript time', () => {
    expect(recordingSecondsFor({ Timestamp: 30, VideoOffset: 41.5 })).toBe(41.5);
    expect(recordingSecondsFor({ Timestamp: 30, VideoOffset: 0 })).toBe(0);
    expect(recordingSecondsFor({ Timestamp: 30, VideoOffset: null })).toBe(30);
    expect(recordingSecondsFor({ Timestamp: 30 })).toBe(30);
  });
});

describe('segmentIndexForTime', () => {
  it('finds the segment that had started by that time', () => {
    const starts = [0, 12.5, 30, 61];
    expect(segmentIndexForTime(starts, 35)).toBe(2);
    expect(segmentIndexForTime(starts, 30)).toBe(2);
    expect(segmentIndexForTime(starts, 1000)).toBe(3);
  });

  it('falls back to the earliest segment for a time before all of them, and -1 for none', () => {
    expect(segmentIndexForTime([12, 4, 20], 1)).toBe(1);
    expect(segmentIndexForTime([], 5)).toBe(-1);
  });

  it('copes with segments listed out of start order', () => {
    // The transcript is sorted by end time, so a long segment can come later.
    expect(segmentIndexForTime([10, 2, 25], 12)).toBe(0);
  });
});
