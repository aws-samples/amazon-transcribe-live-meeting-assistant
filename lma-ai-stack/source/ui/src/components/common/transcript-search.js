/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Finding text in a meeting's transcript, and limiting it to some speakers.
 * Kept free of React so the rules can be tested directly.
 *
 * A search is a set of words: a segment matches when it holds every one, in
 * any order, ignoring case and accents -- the same rule as the search of the
 * shared screen's text on the Screen share tab. Matching segments are marked
 * and stepped through rather than the others being hidden, so each match is
 * read with what was said around it.
 *
 * Messages typed in the meeting chat are searched like what was said. They are
 * not a speaker's lines, so they are shown or hidden by a filter of their own
 * (passesTranscriptFilter) rather than by the speakers chosen.
 */

import { isChatChannel } from './channels';

const DIACRITICS = /[\u0300-\u036f]/g;

/** Lower-case, accents removed, so a search for "resume" finds "Résumé". */
export const foldText = (text) =>
  String(text ?? '')
    .normalize('NFD')
    .replace(DIACRITICS, '')
    .toLowerCase();

/** The words of a search, folded. */
export const searchWords = (query) => foldText(query).split(/\s+/).filter(Boolean);

/** Whether `text` holds every word of `query`. An empty search matches everything. */
export const textMatches = (text, query) => {
  const words = searchWords(query);
  if (!words.length) return true;
  const folded = foldText(text);
  return words.every((word) => folded.includes(word));
};

/**
 * The places in `text` where any word of `query` occurs, as [start, end)
 * ranges into `text` itself (not its folded form), in order and merged where
 * they overlap. Used to mark the words in a matching segment.
 */
export const matchRanges = (text, query) => {
  const words = searchWords(query);
  const source = String(text ?? '');
  if (!words.length || !source) return [];
  // The folded text, with the index in `source` each folded character came from.
  let folded = '';
  const origin = [];
  for (let i = 0; i < source.length; i += 1) {
    const piece = foldText(source[i]);
    for (let j = 0; j < piece.length; j += 1) origin.push(i);
    folded += piece;
  }
  const ranges = [];
  words.forEach((word) => {
    let at = folded.indexOf(word);
    while (at !== -1) {
      ranges.push([origin[at], origin[at + word.length - 1] + 1]);
      at = folded.indexOf(word, at + word.length);
    }
  });
  ranges.sort((a, b) => a[0] - b[0] || a[1] - b[1]);
  return ranges.reduce((merged, range) => {
    const last = merged[merged.length - 1];
    if (last && range[0] <= last[1]) {
      last[1] = Math.max(last[1], range[1]);
    } else {
      merged.push([...range]);
    }
    return merged;
  }, []);
};

/**
 * Plain text cut where it meets `ranges` (as matchRanges gives them, into a
 * longer text this one starts at `offset` of), as [{ text, match }] in order.
 * Used to mark the search's words in text shown as typed, without markdown.
 */
export const splitByRanges = (text, ranges, offset = 0) => {
  const source = String(text ?? '');
  const runs = [];
  let cursor = 0;
  (ranges || []).forEach(([rangeStart, rangeEnd]) => {
    const start = Math.max(rangeStart - offset, cursor);
    const end = Math.min(rangeEnd - offset, source.length);
    if (end <= start) return;
    if (start > cursor) runs.push({ text: source.slice(cursor, start), match: false });
    runs.push({ text: source.slice(start, end), match: true });
    cursor = end;
  });
  if (cursor < source.length) runs.push({ text: source.slice(cursor), match: false });
  return runs;
};

/**
 * A rehype plugin that wraps each occurrence of a search word in <mark>. It
 * runs after sanitising and only splits text nodes, so it adds nothing but
 * `mark` elements around text that was already there. A word split across
 * formatting (half of it bold) is not marked, though its segment still
 * matches.
 */
export const rehypeMarkMatches = (query) => () => (tree) => {
  if (!searchWords(query).length) return;
  const walk = (node) => {
    if (!node?.children) return;
    if (node.type === 'element' && node.tagName === 'mark') return;
    const children = [];
    node.children.forEach((child) => {
      if (child.type !== 'text') {
        walk(child);
        children.push(child);
        return;
      }
      const ranges = matchRanges(child.value, query);
      if (!ranges.length) {
        children.push(child);
        return;
      }
      let cursor = 0;
      ranges.forEach(([start, end]) => {
        if (start > cursor) children.push({ type: 'text', value: child.value.slice(cursor, start) });
        children.push({
          type: 'element',
          tagName: 'mark',
          properties: { className: ['transcript-search-match'] },
          children: [{ type: 'text', value: child.value.slice(start, end) }],
        });
        cursor = end;
      });
      if (cursor < child.value.length) children.push({ type: 'text', value: child.value.slice(cursor) });
    });
    // eslint-disable-next-line no-param-reassign
    node.children = children;
  };
  walk(tree);
};

/**
 * The speakers of a transcript, for its filter: each distinct name shown on a
 * spoken line, in the order they first spoke. `segments` are the displayed
 * segments, with their names already resolved, and `isSpeech(segment)` tells
 * a spoken line from the assistant's.
 */
export const speakerFilterOptions = (segments, isSpeech) => {
  const seen = new Set();
  const names = [];
  [...(segments || [])]
    .filter((segment) => isSpeech(segment))
    .sort((a, b) => (Number(a.startTime) || 0) - (Number(b.startTime) || 0))
    .forEach((segment) => {
      const name = `${segment.speaker ?? ''}`.trim();
      if (name && !seen.has(name)) {
        seen.add(name);
        names.push(name);
      }
    });
  return names;
};

/**
 * Whether a segment is shown under a speaker filter: every segment when no
 * speaker is chosen, and otherwise only the spoken lines of the chosen ones
 * (the assistant's lines are hidden, as nobody chose them).
 */
export const passesSpeakerFilter = (segment, selectedSpeakers, isSpeech) => {
  if (!selectedSpeakers?.length) return true;
  if (!isSpeech(segment)) return false;
  return selectedSpeakers.includes(`${segment.speaker ?? ''}`.trim());
};

/**
 * Whether a segment is shown under the transcript's filter: a chat message
 * when `showChat` is on, whatever speakers are chosen; anything else as
 * passesSpeakerFilter decides.
 */
export const passesTranscriptFilter = (segment, filter, isSpeech) => {
  const { speakers = [], showChat = true } = filter || {};
  if (isChatChannel(segment?.channel)) return Boolean(showChat);
  return passesSpeakerFilter(segment, speakers, isSpeech);
};

/** The match to move to from `current` (an index, or -1 for none yet) by `step` (+1 or -1), wrapping. */
export const stepMatch = (count, current, step) => {
  if (!count) return -1;
  if (current < 0 || current >= count) return step < 0 ? count - 1 : 0;
  return (current + step + count) % count;
};
