/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import { describe, it, expect, vi } from 'vitest';

import {
  HIGHLIGHT_CLASS,
  formatFrameTime,
  frameMatches,
  matchSnippet,
  mergeKeyframes,
  recordingSecondsFor,
  scrollTranscriptToTime,
  segmentIndexForTime,
} from './video-keyframes';

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

describe('scrollTranscriptToTime', () => {
  const transcript = (starts) => {
    const container = document.createElement('div');
    container.scrollTo = vi.fn();
    starts.forEach((start, i) => {
      const row = document.createElement('div');
      row.setAttribute('data-start-time', String(start));
      row.textContent = `segment ${i}`;
      // Each row 50px below the last, measured from a container at y=100 scrolled by 30.
      row.getBoundingClientRect = () => ({ top: 100 - 30 + i * 50 });
      container.appendChild(row);
    });
    container.getBoundingClientRect = () => ({ top: 100 });
    Object.defineProperty(container, 'scrollTop', { value: 30, configurable: true });
    return container;
  };

  it('scrolls to and highlights the segment under way at that time', () => {
    const container = transcript([0, 12, 30, 61]);
    const row = scrollTranscriptToTime(container, 35);
    expect(row.textContent).toBe('segment 2');
    expect(row.classList.contains(HIGHLIGHT_CLASS)).toBe(true);
    // Row 2 sits 100px into the content (its top 170 - container 100 + scrolled 30).
    expect(container.scrollTo).toHaveBeenCalledWith({ top: 92, behavior: 'smooth' });
  });

  it('does nothing for an empty transcript or a time that is not one', () => {
    expect(scrollTranscriptToTime(transcript([]), 5)).toBeNull();
    expect(scrollTranscriptToTime(transcript([0, 10]), NaN)).toBeNull();
    expect(scrollTranscriptToTime(null, 5)).toBeNull();
  });
});

describe('searching slide text', () => {
  const slide = { SlideText: 'Agenda item 2: Support\n- Résumé review and quarterly pipeline budget' };

  it('matches every word in any order, ignoring case and accents', () => {
    expect(frameMatches(slide, 'pipeline RESUME')).toBe(true);
    expect(frameMatches(slide, 'pipeline hiring')).toBe(false);
    expect(frameMatches(slide, '   ')).toBe(true);
    expect(frameMatches({}, 'anything')).toBe(false);
  });

  it('gives the line that matched, shortened around the match', () => {
    expect(matchSnippet(slide, 'support')).toBe('Agenda item 2: Support');
    const snippet = matchSnippet(slide, 'budget', 20);
    expect(snippet).toContain('budget');
    expect(snippet.startsWith('…')).toBe(true);
    expect(matchSnippet(slide, 'nothing')).toBe('');
  });
});
