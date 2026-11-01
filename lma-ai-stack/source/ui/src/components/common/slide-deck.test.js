/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The deck is one page per key frame in time order, with its picture, time,
 * chapter and text; a slide's text is shown as text, never read as markup; and
 * printing waits for the pictures.
 */
import { describe, it, expect, vi } from 'vitest';

import { buildDeck, openDeckTab, printDeck } from './slide-deck';

const frames = [
  { FrameId: 'b', Timestamp: 150, ImageSignedUrl: 'https://signed.example/b.jpg', SlideText: 'Budget\n- Q3' },
  {
    FrameId: 'a',
    Timestamp: 5,
    ThumbnailSignedUrl: 'https://signed.example/a_t.jpg',
    SlideText: '<img src=x onerror=alert(1)>',
  },
];

const freshDocument = () => document.implementation.createHTMLDocument('');

describe('buildDeck', () => {
  it('lays out one section per frame, in time order, with its chapter, time and text', () => {
    const doc = freshDocument();
    const images = buildDeck(doc, { title: 'Weekly sync', frames, chapters: [{ seconds: 148, title: 'Budget' }] });
    expect(doc.title).toBe('Weekly sync - shared screen');
    expect(doc.querySelector('h1').textContent).toBe('Weekly sync');
    const slides = [...doc.querySelectorAll('section.deck-slide')];
    expect(slides).toHaveLength(2);
    expect(slides[0].querySelector('img').getAttribute('src')).toBe('https://signed.example/a_t.jpg');
    expect(slides[1].querySelector('h2').textContent).toBe('Budget');
    expect(slides[1].querySelector('.deck-text').textContent).toBe('Budget\n- Q3');
    expect(images).toHaveLength(2);
  });

  it('shows a slide that holds markup as its text', () => {
    const doc = freshDocument();
    buildDeck(doc, { title: 'm', frames });
    const first = doc.querySelector('section.deck-slide .deck-text');
    expect(first.textContent).toBe('<img src=x onerror=alert(1)>');
    expect(doc.querySelectorAll('img')).toHaveLength(2);
  });
});

describe('printDeck', () => {
  it('fills the tab opened for it and prints it once the pictures are in', async () => {
    const doc = freshDocument();
    const tab = { document: doc, focus: vi.fn(), print: vi.fn() };
    const open = vi.fn(() => tab);
    expect(openDeckTab(open)).toBe(tab);
    expect(open).toHaveBeenCalledWith('', '_blank');
    expect(doc.body.textContent).toBe('Preparing the slides...');
    const done = printDeck(tab, { title: 'm', frames }, { wait: 50 });
    expect(doc.body.textContent).not.toContain('Preparing');
    doc.querySelectorAll('img').forEach((img) => img.dispatchEvent(new Event('load')));
    await done;
    expect(tab.print).toHaveBeenCalledTimes(1);
  });

  it('reports a tab the browser blocked', () => {
    expect(openDeckTab(() => null)).toBeNull();
  });
});
