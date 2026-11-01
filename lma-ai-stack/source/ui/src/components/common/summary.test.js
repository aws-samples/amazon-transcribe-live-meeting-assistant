/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
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
  getTextFileFormattedSummary,
  getTextOnlySummary,
  keyframeImagesAsText,
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
    'SLIDES PRESENTED': '**[00:30] Budget**\n\n![Slide at 00:30](keyframe:0000030000-ab12cd)\n\nBob covered Q3.',
  });

  it('names each picture in words wherever the summary leaves the page', () => {
    expect(getTextFileFormattedSummary(SLIDES)).toBe(
      'SLIDES PRESENTED\n\n[00:30] Budget\n\n(Slide at 00:30)\n\nBob covered Q3.\n\n',
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
