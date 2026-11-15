/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The summary formatters, and in particular the email one.
 *
 * A summary produced from prompt templates is a JSON object with one entry per
 * section; a hand-edited summary is a plain Markdown document, which every one
 * of these functions reaches through its non-JSON branch. The email formatter's
 * result is spliced straight into a `mailto:` URL, so what it returns has to be
 * a percent-encoded body whichever branch produced it.
 */
import { describe, it, expect } from 'vitest';

import {
  getEmailFormattedSummary,
  getMarkdownSummary,
  getMarkdownSummaryWithout,
  getSummarySection,
  getTextFileFormattedSummary,
  getTextOnlySummary,
  keyframeImagesAsText,
  summarySections,
} from './summary';

const SECTIONED = JSON.stringify({
  Summary: 'Sarah Lee walked through the migration plan.',
  'Action Items': 'Sarah to size the backfill.',
});

// What the Edit control saves: Markdown, with the characters a URL cares about.
const HAND_EDITED = [
  '# Migration review',
  '',
  '**Decisions**',
  '',
  '- Move the backfill to Q3 (cost & risk)',
  '- Sarah owns the sizing',
].join('\n');

describe('getEmailFormattedSummary', () => {
  it('encodes a hand-edited multi-line summary, markers and all', () => {
    const body = getEmailFormattedSummary(HAND_EDITED);

    // Nothing that would end the body early or start a new URL parameter.
    expect(body).not.toContain('#');
    expect(body).not.toContain('&');
    expect(body).not.toContain('\n');
    // The `#` heading and the `&` survive, as their encodings.
    expect(body).toContain('%23%20Migration%20review');
    expect(body).toContain('cost%20%26%20risk');
    // Line breaks reach the mail client as CRLF.
    expect(body).toContain('%0D%0A');
    // Markdown bold markers are stripped, as they are for every other export.
    expect(decodeURIComponent(body)).not.toContain('**');
    // And it round-trips to the text somebody typed, bar the bold markers.
    expect(decodeURIComponent(body).replace(/\r\n/g, '\n')).toBe(HAND_EDITED.replace(/\*\*/g, ''));
  });

  it('encodes a sectioned summary the same way', () => {
    const body = getEmailFormattedSummary(SECTIONED);
    expect(body).toContain('%0D%0A');
    expect(body).not.toContain('\n');
    const decoded = decodeURIComponent(body);
    expect(decoded).toContain('Summary');
    expect(decoded).toContain('Sarah Lee walked through the migration plan.');
    expect(decoded).toContain('Action Items');
  });

  it('returns an encoded placeholder when there is no summary', () => {
    expect(getEmailFormattedSummary('')).toBe('Not%20available');
    expect(getEmailFormattedSummary(undefined)).toBe('Not%20available');
  });
});

describe('the other formatters still read a hand-edited summary as itself', () => {
  it('leaves the Markdown alone', () => {
    expect(getMarkdownSummary(HAND_EDITED)).toBe(HAND_EDITED);
    expect(getTextOnlySummary(HAND_EDITED)).toBe(HAND_EDITED);
    expect(getTextFileFormattedSummary(HAND_EDITED)).toBe(HAND_EDITED);
  });
});

describe('a summary with pictures of the shared screen', () => {
  const SLIDES = JSON.stringify({
    'SCREEN SHARE SUMMARY': '**[00:30] Budget**\n\n![Slide at 00:30](keyframe:0000030000-ab12cd)\n\nBob covered Q3.',
  });

  it('names each picture in words wherever the summary leaves the page', () => {
    expect(getTextFileFormattedSummary(SLIDES)).toBe(
      'SCREEN SHARE SUMMARY\n\n[00:30] Budget\n\n(Slide at 00:30)\n\nBob covered Q3.\n\n',
    );
    expect(decodeURIComponent(getEmailFormattedSummary(SLIDES))).toContain('(Slide at 00:30)');
    expect(decodeURIComponent(getEmailFormattedSummary(SLIDES))).not.toContain('keyframe:');
    const edited = 'Notes\n\n![Slide at 01:00](keyframe:f1)';
    expect(getTextFileFormattedSummary(edited)).toBe('Notes\n\n(Slide at 01:00)');
    // The meetings list's preview and popover.
    expect(getTextOnlySummary(edited)).toBe('Notes\n\n(Slide at 01:00)');
    expect(getTextOnlySummary(JSON.stringify({ SUMMARY: 'See ![Slide at 00:30](keyframe:f2)' }))).toBe(
      'See (Slide at 00:30)',
    );
  });

  it('keeps the pictures for the page, and leaves other images and links alone', () => {
    expect(getMarkdownSummary(SLIDES)).toContain('![Slide at 00:30](keyframe:0000030000-ab12cd)');
    const other = '![chart](https://example.com/c.png) [keyframe:x](keyframe:x)';
    expect(keyframeImagesAsText(other)).toBe(other);
  });
});

describe('the sections the meeting page shows in their own place', () => {
  const FULL = JSON.stringify({
    SUMMARY: 'We met.',
    'Agenda Coverage': '- **Budget**: Covered',
    CHAPTERS: '- [00:00] Welcome',
  });

  it('reads one section, by name in any case', () => {
    expect(getSummarySection(FULL, 'AGENDA COVERAGE')).toBe('- **Budget**: Covered');
    expect(getSummarySection(FULL, 'chapters')).toBe('- [00:00] Welcome');
    expect(getSummarySection(FULL, 'ACTIONS')).toBeNull();
    expect(getSummarySection('A hand-written summary.', 'CHAPTERS')).toBeNull();
  });

  it('reads a section of a summary edited by hand, as the editor seeded it', () => {
    // What Save stores after an edit: the generated summary as Markdown.
    const edited = getMarkdownSummary(FULL).replace('We met.', 'We met and agreed the budget.');
    expect(getSummarySection(edited, 'AGENDA COVERAGE')).toBe('- **Budget**: Covered');
    expect(getSummarySection(edited, 'Summary')).toBe('We met and agreed the budget.');
    expect(getSummarySection('## Agenda coverage\n- Hiring: Not covered\n\n## Notes\nOk', 'AGENDA COVERAGE')).toBe(
      '- Hiring: Not covered',
    );
    expect(getSummarySection(edited, 'ACTIONS')).toBeNull();
    // The page shows it beside the agenda, so the summary leaves it out.
    expect(summarySections(edited, ['AGENDA COVERAGE']).map((section) => section.title)).toEqual([
      'SUMMARY',
      'CHAPTERS',
    ]);
  });

  it('renders the summary without the sections named', () => {
    const shown = getMarkdownSummaryWithout(FULL, ['CHAPTERS', 'AGENDA COVERAGE']);
    expect(shown).toBe('**SUMMARY**\n\nWe met.\n\n');
    expect(getMarkdownSummaryWithout(FULL, [])).toBe(getMarkdownSummary(FULL));
  });

  it('keeps a hand-written summary, and a summary that would be left empty, whole', () => {
    expect(getMarkdownSummaryWithout('## Notes\nWe met.', ['CHAPTERS'])).toBe('## Notes\nWe met.');
    const onlyChapters = JSON.stringify({ CHAPTERS: '- [00:00] Welcome' });
    expect(getMarkdownSummaryWithout(onlyChapters, ['CHAPTERS'])).toBe(getMarkdownSummary(onlyChapters));
  });
});

describe('summarySections', () => {
  it('reads a generated summary as its sections, in order', () => {
    expect(summarySections(SECTIONED)).toEqual([
      { title: 'Summary', markdown: 'Sarah Lee walked through the migration plan.' },
      { title: 'Action Items', markdown: 'Sarah to size the backfill.' },
    ]);
  });

  it('leaves out the sections named, by name in any case, unless none would be left', () => {
    const full = JSON.stringify({ SUMMARY: 'We met.', CHAPTERS: '- [00:00] Welcome' });
    expect(summarySections(full, ['chapters']).map((s) => s.title)).toEqual(['SUMMARY']);
    const onlyChapters = JSON.stringify({ CHAPTERS: '- [00:00] Welcome' });
    expect(summarySections(onlyChapters, ['CHAPTERS']).map((s) => s.title)).toEqual(['CHAPTERS']);
  });

  it('splits a hand-written summary at its headings, and at the capitalised bold lines the editor seeds', () => {
    const edited = [
      'Opening words.',
      '',
      '**SUMMARY**',
      '',
      'We met.',
      '',
      '**SCREEN SHARE SUMMARY**',
      '',
      '**[00:30] Budget**',
      '',
      'Bob covered Q3.',
      '',
      '## Next steps',
      '- Ship it',
    ].join('\n');
    expect(summarySections(edited)).toEqual([
      { title: 'Summary', markdown: 'Opening words.' },
      { title: 'SUMMARY', markdown: 'We met.' },
      // A bold line in mixed case is a sub-heading, kept within its section.
      { title: 'SCREEN SHARE SUMMARY', markdown: '**[00:30] Budget**\n\nBob covered Q3.' },
      { title: 'Next steps', markdown: '- Ship it' },
    ]);
    expect(summarySections(edited, ['CHAPTERS'])).toHaveLength(4);
  });

  it.each([
    ['**Chapters**', 'bold, in mixed case'],
    ['#### chapters', 'a level-4 heading, in lower case'],
  ])('makes CHAPTERS a section of its own under %s (%s), so it can be left out', (heading) => {
    const edited = ['**SUMMARY**', '', 'We met.', '', heading, '', '- [00:00] Welcome', '- [05:12] Budget'].join('\n');
    expect(summarySections(edited)).toEqual([
      { title: 'SUMMARY', markdown: 'We met.' },
      { title: 'CHAPTERS', markdown: '- [00:00] Welcome\n- [05:12] Budget' },
    ]);
    // The chapter lines are not left in SUMMARY when the page shows them as the Chapters list.
    expect(summarySections(edited, ['CHAPTERS'])).toEqual([{ title: 'SUMMARY', markdown: 'We met.' }]);
  });

  it('makes AGENDA COVERAGE a section of its own in any case and at any heading level', () => {
    const edited = '**SUMMARY**\n\nWe met.\n\n##### Agenda coverage:\n\n- **Budget**: Covered';
    expect(summarySections(edited, ['AGENDA COVERAGE'])).toEqual([{ title: 'SUMMARY', markdown: 'We met.' }]);
  });

  it('reads a document with no headings as one Summary section, and nothing as no sections', () => {
    expect(summarySections('Just a note.')).toEqual([{ title: 'Summary', markdown: 'Just a note.' }]);
    expect(summarySections(HAND_EDITED).map((s) => s.title)).toEqual(['Migration review']);
    expect(summarySections('')).toEqual([]);
    expect(summarySections(undefined)).toEqual([]);
  });

  it('reads a JSON value that is not an object as Markdown', () => {
    expect(summarySections('42')).toEqual([{ title: 'Summary', markdown: '42' }]);
  });
});
