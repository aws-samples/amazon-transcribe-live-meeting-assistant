/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import { CHAPTERS_HEADING } from './chapters';
import { consentAsText } from './meeting-consent';

// A picture of a key frame in a summary (the screen share summary section), which
// the meeting page shows as the frame itself; see SummaryMarkdown.jsx.
const KEYFRAME_IMAGE = /!\[([^\]\n]*)\]\(keyframe:[A-Za-z0-9_-]{1,64}\)/g;

/** A summary's key-frame pictures as their alt text, for text that leaves the page. */
export const keyframeImagesAsText = (text) =>
  typeof text === 'string' ? text.replace(KEYFRAME_IMAGE, (_match, alt) => `(${alt})`) : text;

export const getTextOnlySummary = (callSummaryText) => {
  if (!callSummaryText) {
    return 'Not available';
  }
  let summary = callSummaryText;
  try {
    const jsonObj = JSON.parse(summary);
    // Do a case-insensitive search for 'summary' in the JSON object keys
    const summaryKey = Object.keys(jsonObj).find((key) => key.toLowerCase() === 'summary');
    if (summaryKey !== undefined) {
      summary = jsonObj[summaryKey];
    } else if (Object.keys(jsonObj).length > 0) {
      // If 'summary' is not found, use the first key as the summary
      summary = Object.keys(jsonObj)[0] || '';
      summary = jsonObj[summary];
    }
  } catch (e) {
    return keyframeImagesAsText(callSummaryText);
  }
  return keyframeImagesAsText(summary);
};

export const getMarkdownSummary = (callSummaryText) => {
  if (!callSummaryText) {
    return 'Not available';
  }
  let summary = callSummaryText;
  try {
    const jsonSummary = JSON.parse(summary);
    summary = '';
    Object.entries(jsonSummary).forEach(([key, value]) => {
      summary += `**${key}**\n\n${value}\n\n`;
    });
  } catch (e) {
    return callSummaryText;
  }
  return summary;
};

/** A section name compared as the summary's prompt template names are: case and spacing aside. */
const sectionName = (name) =>
  String(name || '')
    .trim()
    .toUpperCase();

/**
 * The summary as Markdown, as getMarkdownSummary renders it, without the
 * sections named in `omit` -- for the meeting page, which shows some sections
 * in their own place (the chapters as a list, the agenda coverage beside the
 * agenda). A hand-written summary has no sections and is returned whole, as
 * is a summary that would be left with nothing.
 */
export const getMarkdownSummaryWithout = (callSummaryText, omit = []) => {
  if (!callSummaryText || !omit.length) return getMarkdownSummary(callSummaryText);
  let sections;
  try {
    sections = JSON.parse(callSummaryText);
  } catch (e) {
    return getMarkdownSummary(callSummaryText);
  }
  if (!sections || typeof sections !== 'object') return getMarkdownSummary(callSummaryText);
  const left = new Set(omit.map(sectionName));
  const kept = Object.entries(sections).filter(([key]) => !left.has(sectionName(key)));
  if (!kept.length) return getMarkdownSummary(callSummaryText);
  return getMarkdownSummary(JSON.stringify(Object.fromEntries(kept)));
};

// A line that opens a section of a summary written as one Markdown document: a
// Markdown heading, or a bold line in capitals (`**ACTIONS**`), which is how the
// editor seeds the sections of a generated summary. A bold line with lower-case
// letters (`**[00:30] Budget**`) is a sub-heading within a section, not a section.
const SECTION_HEADING = /^\s*(?:#{1,3}\s+(.+?)\s*#*|\*\*([^*\n]+)\*\*\s*:?)\s*$/;
// The sections the meeting page shows in their own place, named in any case and
// at any heading level: their text must be a section of its own, or it would be
// shown twice (once in the section before it, once in its own place).
const AGENDA_COVERAGE_HEADING = /^\s*(?:#{1,6}\s*)?\**\s*agenda\s+coverage\s*\**\s*:?\s*$/i;

const headingOf = (line) => {
  if (CHAPTERS_HEADING.test(line)) return 'CHAPTERS';
  if (AGENDA_COVERAGE_HEADING.test(line)) return 'AGENDA COVERAGE';
  const match = line.match(SECTION_HEADING);
  if (!match) return null;
  if (match[1]) return match[1].trim();
  const bold = match[2].trim();
  return /[A-Z]/.test(bold) && !/[a-z]/.test(bold) ? bold : null;
};

/**
 * A summary written as one Markdown document, as its sections, split at its
 * headings. With `withHeadings`, each headed section also carries `heading`,
 * its heading line as written, so a download can put a hand-written
 * document's sections in order without rewording them.
 */
const markdownSections = (text, { withHeadings = false } = {}) => {
  // The first entry holds what comes before the first heading, and has no title.
  const sections = [{ title: null, lines: [] }];
  text.split(/\r?\n/).forEach((line) => {
    const heading = headingOf(line);
    if (heading === null) sections[sections.length - 1].lines.push(line);
    else sections.push({ title: heading, heading: line, lines: [] });
  });
  const [preamble, ...headed] = sections.map(({ title, heading, lines }) => ({
    title,
    markdown: lines.join('\n').trim(),
    ...(withHeadings && heading !== undefined ? { heading } : {}),
  }));
  // Text before the first heading, or a document with no heading at all, is a section called Summary.
  return preamble.markdown || !headed.length ? [{ ...preamble, title: 'Summary' }, ...headed] : headed;
};

/**
 * One section of a summary by name (compared as section names are: case and
 * spacing aside), or null when the summary has no such section. A summary
 * produced from several prompt templates is stored as a JSON object keyed by
 * template name; one written or edited by hand is a single Markdown document,
 * read at its headings as summarySections reads it -- so a section the editor
 * was seeded with (`**AGENDA COVERAGE**`) is still found after a hand edit.
 */
export const getSummarySection = (callSummaryText, name) => {
  if (!callSummaryText || typeof callSummaryText !== 'string') return null;
  let sections;
  try {
    sections = JSON.parse(callSummaryText);
  } catch (e) {
    sections = null;
  }
  if (!sections || typeof sections !== 'object' || Array.isArray(sections)) {
    const found = markdownSections(callSummaryText).find((section) => sectionName(section.title) === sectionName(name));
    return found ? found.markdown : null;
  }
  const key = Object.keys(sections).find((k) => sectionName(k) === sectionName(name));
  return key === undefined ? null : String(sections[key] ?? '');
};

/**
 * The summary as its sections, in order: [{ title, markdown }], without the
 * sections named in `omit` (compared as section names are: case and spacing
 * aside). A summary produced from several prompt templates is stored as a JSON
 * object, a section per template; one written by hand is a single Markdown
 * document, split at its headings (one section, "Summary", when it has none).
 * The sections are returned whole when `omit` would leave none. Empty for a
 * meeting with no summary.
 */
export const summarySections = (callSummaryText, omit = []) => {
  if (!callSummaryText || typeof callSummaryText !== 'string') return [];
  let parsed = null;
  try {
    parsed = JSON.parse(callSummaryText);
  } catch (e) {
    parsed = null;
  }
  const all =
    parsed && typeof parsed === 'object' && !Array.isArray(parsed)
      ? Object.entries(parsed).map(([title, value]) => ({ title, markdown: String(value ?? '') }))
      : markdownSections(callSummaryText);
  const left = new Set(omit.map(sectionName));
  const kept = all.filter((section) => !left.has(sectionName(section.title)));
  return kept.length ? kept : all;
};

/**
 * The order a summary's sections are read in, wherever the summary is shown or
 * taken away: the Summary tab and every download. Each entry names a section
 * the shipped prompt templates write, with the other names it is known by (an
 * older name, such as SLIDES PRESENTED, or a common rewording an Admin may
 * have given the template). A section matching none of them -- a custom
 * template -- comes after all of these, in its configured order.
 */
export const SUMMARY_SECTION_ORDER = [
  { name: 'SUMMARY', aliases: ['SUMMARY', 'MEETING SUMMARY', 'EXECUTIVE SUMMARY'] },
  { name: 'ACTIONS', aliases: ['ACTIONS', 'ACTION', 'ACTION ITEMS', 'ACTION ITEM'] },
  { name: 'AGENDA COVERAGE', aliases: ['AGENDA COVERAGE', 'AGENDA'] },
  { name: 'DETAILS', aliases: ['DETAILS', 'DETAIL', 'MEETING DETAILS', 'KEY DETAILS'] },
  { name: 'CHAPTERS', aliases: ['CHAPTERS', 'CHAPTER', 'MEETING CHAPTERS'] },
  {
    name: 'SCREEN SHARE SUMMARY',
    aliases: [
      'SCREEN SHARE SUMMARY',
      'SCREEN SHARE',
      'SCREENSHARE SUMMARY',
      'SCREENSHARE',
      'SHARED SCREEN',
      'SHARED SCREEN SUMMARY',
      'SLIDES PRESENTED',
      'SLIDES',
    ],
  },
  {
    name: 'CHAT & SHARED LINKS',
    aliases: ['CHAT AND SHARED LINKS', 'CHAT SHARED LINKS', 'CHAT AND LINKS', 'CHAT', 'CHAT SUMMARY', 'SHARED LINKS'],
  },
];

/**
 * A section title as the order compares it: in capitals, "&" read as "AND",
 * and every run of anything other than a letter or digit (spaces, "_", ":",
 * "/", the "**" of a bold heading) as one space. "Chat & shared links",
 * "CHAT_AND_SHARED_LINKS" and "**Chat / Shared Links:**" all compare alike.
 */
export const sectionOrderKey = (title) =>
  String(title ?? '')
    .normalize('NFKC')
    .toUpperCase()
    .replace(/&/g, ' AND ')
    .replace(/[^\p{L}\p{N}]+/gu, ' ')
    .trim();

const SECTION_RANKS = new Map(
  SUMMARY_SECTION_ORDER.flatMap(({ aliases }, rank) => aliases.map((alias) => [sectionOrderKey(alias), rank])),
);

/** Where a section comes in SUMMARY_SECTION_ORDER; a custom section after every listed one. */
export const summarySectionRank = (title) => {
  const rank = SECTION_RANKS.get(sectionOrderKey(title));
  return rank === undefined ? SUMMARY_SECTION_ORDER.length : rank;
};

/**
 * Sections ([{ title, ... }]) in reading order (SUMMARY_SECTION_ORDER). The
 * sort is stable: sections of one rank, the custom ones among them, keep the
 * order they were given in, which for a generated summary is the order of
 * the prompt templates. Returns a new array; the sections are not copied.
 */
export const orderSummarySections = (sections) =>
  (sections || [])
    .map((section, index) => ({ section, index, rank: summarySectionRank(section?.title) }))
    .sort((a, b) => a.rank - b.rank || a.index - b.index)
    .map(({ section }) => section);

/** The summary's sections as every download writes them: all of them (summarySections), in reading order. */
export const downloadSummarySections = (callSummaryText) => orderSummarySections(summarySections(callSummaryText));

// The summary's sections when it is stored as JSON, a section per prompt
// template: [[title, value]] in reading order. Null for a summary written by hand.
const orderedJsonEntries = (callSummaryText) => {
  let parsed;
  try {
    parsed = JSON.parse(callSummaryText);
  } catch (e) {
    return null;
  }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return null;
  return orderSummarySections(Object.entries(parsed).map(([title, value]) => ({ title, value }))).map(
    ({ title, value }) => [title, value],
  );
};

// A summary written by hand, with its sections in reading order. Each section
// keeps its heading line and text exactly as written, and a document whose
// sections are already in order is returned exactly as it is.
const orderedMarkdownDocument = (text) => {
  const sections = markdownSections(text, { withHeadings: true });
  const ordered = orderSummarySections(sections);
  if (ordered.every((section, index) => section === sections[index])) return text;
  return ordered.map((section) => [section.heading, section.markdown].filter(Boolean).join('\n\n')).join('\n\n');
};

/**
 * The summary as a percent-encoded `mailto:` body.
 *
 * The text is assembled as plain text and encoded in one pass at the end,
 * rather than written with `%0D%0A` escapes inline. A hand-edited summary is
 * plain Markdown and arrives here through the non-JSON branch, so the body
 * routinely contains characters that carry meaning in a URL -- a `#` opening a
 * heading, an `&` in a sentence -- and each has to reach the mail client as
 * itself rather than as a fragment or a parameter boundary.
 */
export const getEmailFormattedSummary = (callSummaryText) => {
  if (!callSummaryText) {
    return encodeURIComponent('Not available');
  }
  const entries = orderedJsonEntries(callSummaryText);
  const summary = entries
    ? entries.map(([key, value]) => `${key}\n\n${value}\n\n`).join('')
    : orderedMarkdownDocument(callSummaryText);
  // CRLF is the line ending a mail body takes; normalise to LF first so a text
  // that already uses CRLF does not end up with doubled carriage returns.
  const body = keyframeImagesAsText(summary).replace(/\*\*/g, '').replace(/\r\n/g, '\n').replace(/\n/g, '\r\n');
  return encodeURIComponent(body);
};

export const getTextFileFormattedSummary = (callSummaryText) => {
  if (!callSummaryText) {
    return 'Not available';
  }
  // A text file cannot show a key frame, so each is written as its caption.
  // The sections are in reading order (SUMMARY_SECTION_ORDER), as in every download.
  const entries = orderedJsonEntries(callSummaryText);
  if (!entries) return keyframeImagesAsText(orderedMarkdownDocument(callSummaryText));
  return entries
    .map(([key, value]) => `${key}\n\n${keyframeImagesAsText(String(value ?? ''))}\n\n`.replace(/\*\*/g, ''))
    .join('');
};

/**
 * The meeting as plain text: title, date, participants (`participants`, the
 * names the meeting page's header lists, when given), length and summary, and
 * the consent record when the meeting has one (see meeting-consent.js).
 */
export const getTextFileFormattedMeetingDetails = (meeting, consent = null, { participants = [] } = {}) => {
  let meetingDetails = '';
  meetingDetails += `Title: ${meeting.callId}\n`;

  // Not reformatting date in this iteration as it may change timezone from expected
  //  consider adding this in future iterations
  // const date = new Date(call.initiationTimeStamp);
  const date = meeting.initiationTimeStamp;
  meetingDetails += `Date: ${date}\n`;

  const names = (participants || []).map((name) => String(name ?? '').trim()).filter(Boolean);
  if (names.length) meetingDetails += `Participants: ${names.join(', ')}\n`;

  meetingDetails += `Length: ${meeting.conversationDurationTimeStamp}\n`;

  const summary = getTextFileFormattedSummary(meeting.callSummaryText);
  meetingDetails += `\n\n${summary}`;

  if (consent) {
    meetingDetails += `\n\n${consentAsText(consent)}\n`;
  }

  return meetingDetails;
};

export default getTextOnlySummary;
