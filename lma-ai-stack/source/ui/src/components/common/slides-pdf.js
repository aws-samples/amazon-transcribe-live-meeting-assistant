/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The slides as a PDF file built in the browser, for the meeting pack and the
 * email draft (meeting-pack.js): the pages of the deck Screen share › Slides
 * (PDF) prints (slide-deck.js) -- a cover, then one landscape page per frame
 * with its label (its title, else the chapter it begins), its picture, its
 * time and its text -- with the frames the deck leaves out left out here too
 * (the camera's, and repeats of an earlier frame).
 *
 * Each picture is fetched through the signed link getVideoKeyframes returned
 * (fetchImageBlob, as the summary's thumbnails are), scaled to at most
 * SLIDE_WIDTH pixels wide and re-encoded as a JPEG, so a meeting with
 * hundreds of frames stays a size a mail server takes more often. A picture
 * that cannot be fetched is written as a line saying so.
 */
import { chapterTitlesByFrame } from './chapters';
import { coverBlocks } from './pdf-cover';
import { buildPdf } from './pdf-export';
import { downscaleToJpeg, fetchImageBlob } from './keyframe-thumbnails';
import { formatFrameTime, frameLabel, isDistinctFrame } from './video-keyframes';

/** The width, in pixels, each slide's picture is scaled down to (never up). */
export const SLIDE_WIDTH = 1600;
const CONCURRENT_FETCHES = 4;
const GREY = '5f6b7a';

/** The frames the deck shows, in time order. */
export const deckFrames = (frames) =>
  (frames || []).filter(isDistinctFrame).sort((a, b) => Number(a.Timestamp) - Number(b.Timestamp));

const forEachLimited = async (items, limit, work) => {
  let next = 0;
  const worker = async () => {
    while (next < items.length) {
      const index = next;
      next += 1;
      // eslint-disable-next-line no-await-in-loop -- each worker takes one item at a time
      await work(items[index], index);
    }
  };
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, worker));
};

/**
 * Each frame's picture, in order (null for one that could not be fetched).
 * `onProgress(done, total)` is told as each one arrives.
 */
export const fetchSlidePictures = async (
  frames,
  { fetchImage = fetchImageBlob, resize = downscaleToJpeg, onProgress = () => {} } = {},
) => {
  const pictures = new Array(frames.length).fill(null);
  let done = 0;
  await forEachLimited(frames, CONCURRENT_FETCHES, async (frame, index) => {
    const links = [frame.ImageSignedUrl, frame.ThumbnailSignedUrl].filter(Boolean);
    for (let i = 0; i < links.length; i += 1) {
      try {
        // eslint-disable-next-line no-await-in-loop -- the thumbnail is tried only when the picture fails
        pictures[index] = await resize(await fetchImage(links[i]), SLIDE_WIDTH);
        break;
      } catch (error) {
        pictures[index] = null;
      }
    }
    done += 1;
    onProgress(done, frames.length);
  });
  return pictures;
};

/** The deck's blocks (pdf-export.js): the cover, then a page per frame. */
export const slidesPdfBlocks = ({ title, facts = [], note = '', frames = [], chapters = [], pictures = [] }) => {
  const chapterTitles = chapterTitlesByFrame(chapters, frames);
  const blocks = coverBlocks({
    kind: 'Slides',
    title,
    facts: [
      ...facts,
      { label: 'Slides', value: `${frames.length} ${frames.length === 1 ? 'frame' : 'frames'} of the shared screen` },
    ],
    note,
  });
  frames.forEach((frame, index) => {
    const time = formatFrameTime(frame.Timestamp);
    blocks.push({ kind: 'pageBreak' });
    const label = frameLabel(frame, chapterTitles);
    blocks.push({ kind: 'heading', level: 2, runs: [{ text: label || `Slide at ${time}` }] });
    const picture = pictures[index];
    if (picture) blocks.push({ kind: 'image', image: picture, id: frame.FrameId, center: true, maxHeight: 400 });
    else
      blocks.push({
        kind: 'paragraph',
        runs: [{ text: '(The picture could not be fetched.)', italic: true, color: GREY }],
      });
    blocks.push({ kind: 'paragraph', size: 9, runs: [{ text: time, color: GREY }] });
    if (frame.SlideText)
      blocks.push({ kind: 'paragraph', size: 9, runs: [{ text: frame.SlideText, color: '333333' }] });
  });
  return blocks;
};

/**
 * The slides PDF (a jsPDF document) for `frames` (every key frame the
 * meeting has; the deck's are chosen here). `onProgress(done, total)` follows
 * the pictures being fetched.
 */
export const buildSlidesPdf = async (
  { title, facts = [], note = '', frames = [], chapters = [] },
  { jsPDF, rasterizeText = null, fetchImage, resize, onProgress = () => {}, compress = true } = {},
) => {
  const ordered = deckFrames(frames);
  const pictures = await fetchSlidePictures(ordered, { fetchImage, resize, onProgress });
  const blocks = slidesPdfBlocks({ title, facts, note, frames: ordered, chapters, pictures });
  return buildPdf(blocks, {
    jsPDF,
    compress,
    orientation: 'landscape',
    title: `${title} - Slides`,
    footer: `${title}  ·  Slides`,
    rasterizeText,
  });
};
