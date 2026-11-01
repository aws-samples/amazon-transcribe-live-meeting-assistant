/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The meeting's key frames as a printable deck: one page per frame, landscape,
 * with its time, the chapter it begins (if any) and its text, in a new tab with
 * the browser's print dialog, where **Save as PDF** makes the PDF. See
 * docs/meeting-video.md.
 *
 * The tab is opened by openDeckTab within the click itself: browsers allow a
 * new tab only while the click still counts as the user's, which an await for
 * the frames would end. printDeck fills it once the frames are read.
 *
 * Printing through the browser rather than building the PDF here keeps every
 * script and language the slides use (the browser has the fonts; a PDF library
 * embeds only what it ships) and adds no dependency. Everything is built with
 * DOM calls and textContent, so a slide's text is never read as markup.
 */
import { chapterTitlesByFrame } from './chapters';
import { formatFrameTime } from './video-keyframes';

const DECK_STYLE = `
@page { size: landscape; margin: 10mm; }
body { font-family: system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif; margin: 0; color: #111; }
.deck-cover { padding: 24px 0; }
.deck-cover h1 { font-size: 20px; margin: 0 0 4px; }
.deck-slide { break-after: page; page-break-after: always; padding: 8px 0; }
.deck-slide:last-child { break-after: auto; page-break-after: auto; }
.deck-slide h2 { font-size: 16px; margin: 0 0 6px; }
.deck-slide img { display: block; max-width: 100%; max-height: 150mm; margin: 0 auto 8px; border: 1px solid #ccc; }
.deck-time { font-size: 12px; color: #555; margin: 0 0 4px; }
.deck-text { font-size: 11px; white-space: pre-wrap; margin: 0; color: #333; }
@media screen { body { max-width: 1100px; margin: 0 auto; padding: 16px; } .deck-slide { border-bottom: 1px solid #ddd; } }
`;

const element = (doc, tag, className, text) => {
  const el = doc.createElement(tag);
  if (className) el.className = className;
  if (text !== undefined) el.textContent = text;
  return el;
};

/**
 * A new tab for the deck, opened now (call it from the click, before any
 * await), showing that the deck is being prepared. Null when the browser
 * blocked it.
 */
export const openDeckTab = (open = window.open) => {
  const tab = open('', '_blank');
  if (!tab) return null;
  tab.document.title = 'Preparing the slides';
  tab.document.body.appendChild(element(tab.document, 'p', null, 'Preparing the slides...'));
  return tab;
};

/** Fills `doc` with the deck, replacing what it shows. Returns its images, for the caller to wait on. */
export const buildDeck = (doc, { title, frames, chapters = [] }) => {
  const ordered = [...(frames || [])].sort((a, b) => Number(a.Timestamp) - Number(b.Timestamp));
  const chapterTitles = chapterTitlesByFrame(chapters, ordered);
  // eslint-disable-next-line no-param-reassign
  doc.title = `${title} - shared screen`;
  doc.body.replaceChildren();
  doc.head.appendChild(element(doc, 'style', null, DECK_STYLE));
  const cover = element(doc, 'header', 'deck-cover');
  cover.appendChild(element(doc, 'h1', null, title));
  cover.appendChild(
    element(
      doc,
      'p',
      'deck-time',
      `${ordered.length} ${ordered.length === 1 ? 'frame' : 'frames'} of the shared screen`,
    ),
  );
  doc.body.appendChild(cover);
  const images = [];
  ordered.forEach((frame) => {
    const time = formatFrameTime(frame.Timestamp);
    const slide = element(doc, 'section', 'deck-slide');
    (chapterTitles.get(frame.FrameId) || []).forEach((chapter) => slide.appendChild(element(doc, 'h2', null, chapter)));
    const src = frame.ImageSignedUrl || frame.ThumbnailSignedUrl;
    if (src) {
      const img = element(doc, 'img');
      img.src = src;
      img.alt = `Shared screen at ${time}`;
      slide.appendChild(img);
      images.push(img);
    }
    slide.appendChild(element(doc, 'p', 'deck-time', time));
    if (frame.SlideText) slide.appendChild(element(doc, 'p', 'deck-text', frame.SlideText));
    doc.body.appendChild(slide);
  });
  return images;
};

const IMAGE_WAIT_MS = 20000;

const loaded = (img, wait) =>
  img.complete
    ? Promise.resolve()
    : new Promise((resolve) => {
        let timer = null;
        const done = () => {
          clearTimeout(timer);
          resolve();
        };
        img.addEventListener('load', done, { once: true });
        img.addEventListener('error', done, { once: true });
        timer = setTimeout(done, wait);
      });

/**
 * Fills a tab from openDeckTab with the deck and, once its pictures have
 * loaded (or after IMAGE_WAIT_MS), opens the print dialog.
 */
export const printDeck = async (tab, { title, frames, chapters }, { wait = IMAGE_WAIT_MS } = {}) => {
  const images = buildDeck(tab.document, { title, frames, chapters });
  await Promise.all(images.map((img) => loaded(img, wait)));
  tab.focus();
  tab.print();
};
