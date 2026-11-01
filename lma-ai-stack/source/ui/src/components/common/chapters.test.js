/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Chapters are read from the CHAPTERS section of a summary, generated or
 * edited by hand; a line that cannot be read is left out, and a chapter marks
 * the key frame that went up with it, if any.
 */
import { describe, it, expect } from 'vitest';

import { chapterTitlesByFrame, chaptersFromSummary, parseChapters, parseClock } from './chapters';

describe('parseClock', () => {
  it('reads MM:SS and H:MM:SS, and nothing else', () => {
    expect(parseClock('05:12')).toBe(312);
    expect(parseClock('1:02:03')).toBe(3723);
    expect(parseClock('75:00')).toBe(4500);
    expect(parseClock('5:61')).toBeNull();
    expect(parseClock('1:61:00')).toBeNull();
    expect(parseClock('12')).toBeNull();
    expect(parseClock('a:bc')).toBeNull();
  });
});

describe('parseChapters', () => {
  it('reads the shipped format and the common variations, in time order, one per time', () => {
    const text = [
      '- [05:12] Budget review',
      '- [00:00] Welcome',
      '1. **[1:02:03]** - Hiring plan',
      '* 10:00: Roadmap',
      '- [05:12] A second chapter at the same time',
      '- [xx:yy] Not a time',
      'Some prose with no time',
      '- [12:00]',
    ].join('\n');
    expect(parseChapters(text)).toEqual([
      { seconds: 0, label: '00:00', title: 'Welcome' },
      { seconds: 312, label: '05:12', title: 'Budget review' },
      { seconds: 600, label: '10:00', title: 'Roadmap' },
      { seconds: 3723, label: '1:02:03', title: 'Hiring plan' },
    ]);
  });
});

describe('chaptersFromSummary', () => {
  it('reads the CHAPTERS section of a generated summary', () => {
    const summary = JSON.stringify({
      SUMMARY: '- [01:00] not a chapter',
      CHAPTERS: '- [00:00] Welcome\n- [02:30] Budget',
    });
    expect(chaptersFromSummary(summary).map((c) => c.title)).toEqual(['Welcome', 'Budget']);
  });

  it('reads the section under its heading once the summary was edited by hand', () => {
    const edited = [
      '**SUMMARY**',
      '',
      'At [01:00] we met.',
      '',
      '**CHAPTERS**',
      '',
      '- [00:00] Welcome',
      '- [02:30] Budget',
      '',
      '**ACTIONS**',
      '',
      '- [03:00] Not a chapter',
    ].join('\n');
    expect(chaptersFromSummary(edited).map((c) => c.title)).toEqual(['Welcome', 'Budget']);
  });

  it('finds none without the section', () => {
    expect(chaptersFromSummary(JSON.stringify({ SUMMARY: '- [00:00] x' }))).toEqual([]);
    expect(chaptersFromSummary('Just text')).toEqual([]);
    expect(chaptersFromSummary(null)).toEqual([]);
  });
});

describe('chapterTitlesByFrame', () => {
  it('marks the frame that went up closest to each chapter, within 30 seconds', () => {
    const frames = [
      { FrameId: 'a', Timestamp: 0 },
      { FrameId: 'b', Timestamp: 148 },
      { FrameId: 'c', Timestamp: 300 },
    ];
    const chapters = [
      { seconds: 0, title: 'Welcome' },
      { seconds: 150, title: 'Budget' },
      { seconds: 220, title: 'A topic with no new slide' },
      { seconds: 290, title: 'Hiring' },
    ];
    const titles = chapterTitlesByFrame(chapters, frames);
    expect(Object.fromEntries(titles)).toEqual({ a: ['Welcome'], b: ['Budget'], c: ['Hiring'] });
  });
});
