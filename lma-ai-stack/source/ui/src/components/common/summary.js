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

/** A summary written as one Markdown document, as its sections, split at its headings. */
const markdownSections = (text) => {
  // The first entry holds what comes before the first heading, and has no title.
  const sections = [{ title: null, lines: [] }];
  text.split(/\r?\n/).forEach((line) => {
    const heading = headingOf(line);
    if (heading === null) sections[sections.length - 1].lines.push(line);
    else sections.push({ title: heading, lines: [] });
  });
  const [preamble, ...headed] = sections.map(({ title, lines }) => ({ title, markdown: lines.join('\n').trim() }));
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
  let summary = callSummaryText;
  try {
    const jsonSummary = JSON.parse(summary);
    summary = '';
    Object.entries(jsonSummary).forEach(([key, value]) => {
      summary += `${key}\n\n${value}\n\n`;
    });
  } catch (e) {
    summary = callSummaryText;
  }
  // CRLF is the line ending a mail body takes; normalise to LF first so a text
  // that already uses CRLF does not end up with doubled carriage returns.
  const body = keyframeImagesAsText(summary).replace(/\*\*/g, '').replace(/\r\n/g, '\n').replace(/\n/g, '\r\n');
  return encodeURIComponent(body);
};

export const getTextFileFormattedSummary = (callSummaryText) => {
  if (!callSummaryText) {
    return 'Not available';
  }
  // The copy, the text file and the Word document cannot show a key frame.
  let summary = keyframeImagesAsText(callSummaryText);
  try {
    const jsonSummary = JSON.parse(summary);
    summary = '';
    Object.entries(jsonSummary).forEach(([key, value]) => {
      summary += `${key}\n\n${value}\n\n`;
      summary = summary.replace(/\*\*/g, '');
    });
  } catch (e) {
    return keyframeImagesAsText(callSummaryText);
  }
  return summary;
};

/**
 * The meeting as plain text: title, date, length and summary, and the consent
 * record when the meeting has one (see meeting-consent.js).
 */
export const getTextFileFormattedMeetingDetails = (meeting, consent = null) => {
  let meetingDetails = '';
  meetingDetails += `Title: ${meeting.callId}\n`;

  // Not reformatting date in this iteration as it may change timezone from expected
  //  consider adding this in future iterations
  // const date = new Date(call.initiationTimeStamp);
  const date = meeting.initiationTimeStamp;
  meetingDetails += `Date: ${date}\n`;

  meetingDetails += `Length: ${meeting.conversationDurationTimeStamp}\n`;

  const summary = getTextFileFormattedSummary(meeting.callSummaryText);
  meetingDetails += `\n\n${summary}`;

  if (consent) {
    meetingDetails += `\n\n${consentAsText(consent)}\n`;
  }

  return meetingDetails;
};

export default getTextOnlySummary;
