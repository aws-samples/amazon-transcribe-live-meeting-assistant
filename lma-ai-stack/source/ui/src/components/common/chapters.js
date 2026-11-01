/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The meeting's chapters, read from the CHAPTERS section of its summary: one
 * line per chapter, "- [MM:SS] Title" (or [H:MM:SS]), on the transcript's
 * clock. See docs/meeting-video.md.
 *
 * The section is model output, and an Admin can edit the prompt or the summary
 * by hand, so the reading is lenient about list marks, brackets and bold, and
 * a line it cannot read is left out rather than guessed at.
 */

// "- [05:12] Budget", "1. **[1:02:03]** - Hiring", "05:12: Budget", "* 5:12 Budget".
const CHAPTER_LINE =
  /^\s*(?:[-*•]\s*|\d{1,3}[.)]\s+)?\**\s*\[?((?:\d{1,2}:)?\d{1,3}:\d{2})\]?\s*\**\s*(?:[-–—:|]\s*)?(.*?)\s*$/;
// A heading that names the section in a hand-edited (Markdown) summary.
const CHAPTERS_HEADING = /^\s*(?:#{1,6}\s*)?\**\s*chapters\s*\**\s*:?\s*$/i;
// Any other heading, which ends the section.
const HEADING = /^\s*(?:#{1,6}\s+\S|\*\*[^*]+\*\*\s*:?\s*$)/;

export const MAX_CHAPTERS = 50;

/** Seconds from "MM:SS" or "H:MM:SS", or null. */
export const parseClock = (text) => {
  const parts = String(text || '')
    .split(':')
    .map((p) => Number(p));
  if (parts.length < 2 || parts.length > 3 || parts.some((p) => !Number.isFinite(p) || p < 0)) return null;
  const [h, m, s] = parts.length === 3 ? parts : [0, ...parts];
  if (s >= 60 || (parts.length === 3 && m >= 60)) return null;
  return h * 3600 + m * 60 + s;
};

const cleanTitle = (title) =>
  String(title || '')
    .replace(/\*\*/g, '')
    .replace(/^[\s\-–—:|]+/, '')
    .trim();

/** The chapters in a block of text, in time order, one per distinct time. */
export const parseChapters = (text) => {
  if (typeof text !== 'string') return [];
  const byTime = new Map();
  text.split(/\r?\n/).forEach((line) => {
    const match = CHAPTER_LINE.exec(line);
    if (!match) return;
    const seconds = parseClock(match[1]);
    const title = cleanTitle(match[2]);
    if (seconds === null || !title || byTime.has(seconds)) return;
    byTime.set(seconds, { seconds, label: match[1], title });
  });
  return [...byTime.values()].sort((a, b) => a.seconds - b.seconds).slice(0, MAX_CHAPTERS);
};

/** The section of a hand-edited summary under its CHAPTERS heading. */
const markdownSection = (text) => {
  const lines = text.split(/\r?\n/);
  const start = lines.findIndex((line) => CHAPTERS_HEADING.test(line));
  if (start < 0) return '';
  const body = [];
  for (let i = start + 1; i < lines.length; i += 1) {
    if (HEADING.test(lines[i]) && !CHAPTER_LINE.test(lines[i])) break;
    body.push(lines[i]);
  }
  return body.join('\n');
};

/** The meeting's chapters from its stored summary (JSON sections, or Markdown once edited). */
export const chaptersFromSummary = (callSummaryText) => {
  if (!callSummaryText || typeof callSummaryText !== 'string') return [];
  let sections = null;
  try {
    sections = JSON.parse(callSummaryText);
  } catch (e) {
    return parseChapters(markdownSection(callSummaryText));
  }
  if (!sections || typeof sections !== 'object') return [];
  const key = Object.keys(sections).find((k) => k.trim().toLowerCase() === 'chapters');
  return key ? parseChapters(String(sections[key] ?? '')) : [];
};

/** How far apart a chapter's time and a frame's may be for the frame to mark the chapter. */
export const CHAPTER_FRAME_WINDOW_SECONDS = 30;

/**
 * Which key frame marks each chapter on the strip: the frame that went up
 * closest to the chapter's time, within CHAPTER_FRAME_WINDOW_SECONDS (a new
 * slide and the line that begins its topic rarely share a second). A chapter
 * that begins with no change of the shared screen marks no frame.
 * FrameId -> chapter titles.
 */
export const chapterTitlesByFrame = (chapters, frames) => {
  const titles = new Map();
  if (!chapters?.length || !frames?.length) return titles;
  chapters.forEach((chapter) => {
    let best = null;
    let bestGap = Infinity;
    frames.forEach((frame) => {
      const gap = Math.abs(Number(frame.Timestamp) - chapter.seconds);
      if (gap <= CHAPTER_FRAME_WINDOW_SECONDS && gap < bestGap) {
        best = frame;
        bestGap = gap;
      }
    });
    if (best) titles.set(best.FrameId, [...(titles.get(best.FrameId) || []), chapter.title]);
  });
  return titles;
};
