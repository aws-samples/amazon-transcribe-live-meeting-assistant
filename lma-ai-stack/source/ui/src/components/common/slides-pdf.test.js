/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The Slides PDF of the meeting pack: the deck's frames in time order, each
 * picture fetched (its thumbnail when the picture fails), and the pages a
 * frame gets.
 */
import { describe, it, expect, vi } from 'vitest';
import { jsPDF } from 'jspdf';

import { SLIDE_WIDTH, buildSlidesPdf, deckFrames, fetchSlidePictures, slidesPdfBlocks } from './slides-pdf';

const PICTURE = { data: new Uint8Array([0xff, 0xd8, 0xff]), width: 1600, height: 900 };
const frame = (id, seconds, extra = {}) => ({
  FrameId: id,
  Timestamp: seconds,
  ImageSignedUrl: `https://img/${id}`,
  ThumbnailSignedUrl: `https://thumb/${id}`,
  ...extra,
});
const text = (block) => (block.runs || []).map((run) => run.text).join('');

describe('deckFrames', () => {
  it("keeps the shared screen's distinct frames, in time order", () => {
    const frames = [
      frame('b', 20),
      frame('cam', 5, { ContentKind: 'CAMERA' }),
      frame('a', 10),
      frame('dup', 30, { DuplicateOf: 'a' }),
    ];
    expect(deckFrames(frames).map((f) => f.FrameId)).toEqual(['a', 'b']);
  });
});

describe('fetchSlidePictures', () => {
  it('fetches each picture, scaled to the slide width, with its progress', async () => {
    const fetchImage = vi.fn(async (url) => url);
    const resize = vi.fn(async () => PICTURE);
    const onProgress = vi.fn();
    const pictures = await fetchSlidePictures([frame('a', 1), frame('b', 2)], { fetchImage, resize, onProgress });
    expect(pictures).toEqual([PICTURE, PICTURE]);
    expect(resize).toHaveBeenCalledWith('https://img/a', SLIDE_WIDTH);
    expect(onProgress).toHaveBeenLastCalledWith(2, 2);
  });

  it('falls back to the thumbnail, and to nothing when neither can be fetched', async () => {
    const fetchImage = vi.fn(async (url) => {
      if (url.startsWith('https://img/')) throw new Error('403');
      if (url === 'https://thumb/b') throw new Error('403');
      return url;
    });
    const resize = vi.fn(async (blob) => ({ ...PICTURE, from: blob }));
    const pictures = await fetchSlidePictures([frame('a', 1), frame('b', 2)], { fetchImage, resize });
    expect(pictures[0].from).toBe('https://thumb/a');
    expect(pictures[1]).toBeNull();
  });
});

describe('slidesPdfBlocks', () => {
  it('opens with a cover, then gives each frame a page with its label, picture, time and text', () => {
    const frames = [frame('a', 30, { Title: 'Budget', SlideText: 'Q3 numbers' }), frame('b', 90)];
    const blocks = slidesPdfBlocks({ title: 'Weekly sync', frames, pictures: [PICTURE, null] });
    expect(blocks.filter((block) => block.kind === 'pageBreak')).toHaveLength(2);
    const pages = blocks.slice(blocks.findIndex((block) => block.kind === 'pageBreak'));
    const shown = pages.map((block) => (block.kind === 'image' ? `image:${block.id}` : text(block))).filter(Boolean);
    expect(shown).toEqual([
      'Budget',
      'image:a',
      '0:30',
      'Q3 numbers',
      'Slide at 1:30',
      '(The picture could not be fetched.)',
      '1:30',
    ]);
    expect(blocks.map(text)).toContain('Slides: 2 frames of the shared screen');
  });
});

describe('buildSlidesPdf', () => {
  it('lays the deck out on landscape pages, a cover and a page per frame', async () => {
    const addImage = vi.spyOn(jsPDF.API, 'addImage').mockImplementation(function addImage() {
      return this;
    });
    const doc = await buildSlidesPdf(
      { title: 'Weekly sync', frames: [frame('b', 20), frame('a', 10), frame('c', 5, { ContentKind: 'CAMERA' })] },
      { jsPDF, compress: false, fetchImage: async (url) => url, resize: async () => PICTURE },
    );
    expect(doc.getNumberOfPages()).toBe(3);
    expect(doc.internal.pageSize.getWidth()).toBeGreaterThan(doc.internal.pageSize.getHeight());
    expect(addImage).toHaveBeenCalledTimes(2);
    const strings = [...doc.output().matchAll(/\(((?:\\.|[^\\)])*)\) Tj/g)].map((m) => m[1]);
    expect(strings).toEqual(expect.arrayContaining(['SLIDES', 'Weekly sync', 'Slide at 0:10', 'Page 3 of 3']));
    addImage.mockRestore();
  });
});
