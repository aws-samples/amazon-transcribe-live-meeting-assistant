/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Every summary download holds every section of the summary, in one reading
 * order: SUMMARY, ACTIONS, AGENDA COVERAGE, DETAILS, CHAPTERS, SCREEN SHARE
 * SUMMARY, CHAT & SHARED LINKS, then any custom section in its configured
 * order. Checked for each format the Download menu offers (Markdown and its
 * copy, Word and PDF, the web page, plain text, email) and for the Summary tab.
 */
import { describe, it, expect } from 'vitest';
import { jsPDF } from 'jspdf';

import {
  downloadSummarySections,
  getEmailFormattedSummary,
  getTextFileFormattedMeetingDetails,
  orderSummarySections,
  sectionOrderKey,
  SUMMARY_SECTION_ORDER,
  summarySectionRank,
  summarySections,
} from './summary';
import { summaryDocBlocks, summaryDownloadHtml, summaryDownloadMarkdown } from './download-func';
import { buildPdf } from './pdf-export';
import * as htmlExport from './html-export';

const READING_ORDER = [
  'SUMMARY',
  'ACTIONS',
  'AGENDA COVERAGE',
  'DETAILS',
  'CHAPTERS',
  'SCREEN SHARE SUMMARY',
  'CHAT & SHARED LINKS',
  'RISKS',
  'CUSTOMER QUOTES',
];

// As the summary Lambda stores it: one key per prompt template, in the
// templates' order, with two custom templates among the shipped ones.
const STORED = JSON.stringify({
  SUMMARY: 'Sarah walked through the migration plan.',
  RISKS: 'Backfill may slip a sprint.',
  DETAILS: '- The backfill moves to Q3.',
  ACTIONS: '- **Sarah**: size the backfill.',
  'AGENDA COVERAGE': '- **[Budget]**: Covered - approved.',
  'SCREEN SHARE SUMMARY': '**[00:30] Budget**\n\n![Slide at 00:30](keyframe:0000030000-ab12cd)\n\nQ3 numbers.',
  CHAPTERS: '- [00:00] Welcome\n- [02:10] Budget',
  'CUSTOMER QUOTES': '- "Ship it."',
  'CHAT & SHARED LINKS': '- **Docs**: shared the plan.',
});
const MEETING = {
  callId: 'Weekly sync-2026-11-03-14-05-09-123',
  initiationTimeStamp: '2026-11-03T14:05:09.123Z',
  conversationDurationTimeStamp: '00:17:16',
  callSummaryText: STORED,
};
// One distinctive line of each section's text, to find the section by.
const BODY_MARKERS = {
  SUMMARY: 'Sarah walked through the migration plan.',
  ACTIONS: 'size the backfill.',
  'AGENDA COVERAGE': 'Covered - approved.',
  DETAILS: 'The backfill moves to Q3.',
  CHAPTERS: 'Welcome',
  'SCREEN SHARE SUMMARY': 'Q3 numbers.',
  'CHAT & SHARED LINKS': 'shared the plan.',
  RISKS: 'Backfill may slip a sprint.',
  'CUSTOMER QUOTES': 'Ship it.',
};
const noThumbnails = async () => ({});

// Where each title is found in `text`, in reading order: each must be there, after the one before it.
const expectInOrder = (text, titles) => {
  let from = 0;
  titles.forEach((title) => {
    const at = text.indexOf(title, from);
    expect(at, `"${title}" after position ${from}`).toBeGreaterThanOrEqual(0);
    from = at + title.length;
  });
};

describe('the reading order', () => {
  it('lists the shipped sections in reading order', () => {
    expect(SUMMARY_SECTION_ORDER.map((entry) => entry.name)).toEqual(READING_ORDER.slice(0, 7));
  });

  it('puts the stored sections in reading order, with custom ones after, in their configured order', () => {
    expect(downloadSummarySections(STORED).map((section) => section.title)).toEqual(READING_ORDER);
  });

  it('matches section names whatever their case, spacing, punctuation or older name', () => {
    const titles = [
      'Customer quotes',
      'chat_and_shared_links',
      'Slides Presented',
      '**Chapters:**',
      'details',
      'Agenda  Coverage',
      'Action Items',
      'Meeting Summary',
    ];
    const ordered = orderSummarySections(titles.map((title) => ({ title }))).map((section) => section.title);
    expect(ordered).toEqual([
      'Meeting Summary',
      'Action Items',
      'Agenda  Coverage',
      'details',
      '**Chapters:**',
      'Slides Presented',
      'chat_and_shared_links',
      'Customer quotes',
    ]);
    expect(sectionOrderKey('Chat & Shared Links')).toBe(sectionOrderKey('CHAT AND SHARED LINKS'));
    expect(summarySectionRank('Screen-share summary')).toBe(summarySectionRank('SCREEN SHARE SUMMARY'));
  });

  it('never takes a section whose name only contains a shipped name for that section', () => {
    expect(summarySectionRank('SCREEN SHARE SUMMARY')).not.toBe(summarySectionRank('SUMMARY'));
    expect(summarySectionRank('CHAPTERS OVERVIEW')).toBe(SUMMARY_SECTION_ORDER.length);
    expect(summarySectionRank('')).toBe(SUMMARY_SECTION_ORDER.length);
  });

  it('keeps sections of one rank in the order given, and leaves the input alone', () => {
    const input = [{ title: 'Z custom' }, { title: 'SUMMARY' }, { title: 'A custom' }, { title: 'Summary' }];
    const copy = [...input];
    expect(orderSummarySections(input).map((section) => section.title)).toEqual([
      'SUMMARY',
      'Summary',
      'Z custom',
      'A custom',
    ]);
    expect(input).toEqual(copy);
  });

  it('orders a summary written by hand at its headings, its preamble as SUMMARY', () => {
    const edited = [
      'The plan was agreed.',
      '',
      '**CHAPTERS**',
      '',
      '- [00:00] Welcome',
      '',
      '**ACTIONS**',
      '',
      '- Sarah sizes the backfill.',
    ].join('\n');
    expect(downloadSummarySections(edited).map((section) => section.title)).toEqual(['Summary', 'ACTIONS', 'CHAPTERS']);
    // The plain-text download moves the sections, each with its heading line as written.
    expect(getTextFileFormattedMeetingDetails({ callId: 'm', callSummaryText: edited })).toContain(
      'The plan was agreed.\n\n**ACTIONS**\n\n- Sarah sizes the backfill.\n\n**CHAPTERS**\n\n- [00:00] Welcome',
    );
  });
});

describe('every summary format holds every section, in reading order', () => {
  it('the Markdown download and its copy', () => {
    const markdown = summaryDownloadMarkdown(MEETING);
    expectInOrder(
      markdown,
      READING_ORDER.map((title) => `## ${title}`),
    );
    READING_ORDER.forEach((title) => expect(markdown).toContain(BODY_MARKERS[title]));
  });

  it('the Word and PDF documents', async () => {
    const blocks = await summaryDocBlocks(MEETING, null, { loadThumbnails: noThumbnails });
    const headings = blocks
      .filter((block) => block.kind === 'heading' && block.level === 2)
      .map((block) => block.runs.map((run) => run.text).join(''));
    expect(headings).toEqual(READING_ORDER);
    const text = blocks.map((block) => (block.runs || []).map((run) => run.text).join('')).join('\n');
    READING_ORDER.forEach((title) => expect(text).toContain(BODY_MARKERS[title]));

    // And the PDF built from those blocks writes them all, in that order.
    const doc = await buildPdf(blocks, { jsPDF, compress: false });
    expectInOrder(
      doc.output(),
      READING_ORDER.map((title) => `(${title})`),
    );
  });

  it('the web page', async () => {
    const html = await summaryDownloadHtml(MEETING, null, {
      loadThumbnails: noThumbnails,
      loadRenderer: async () => htmlExport,
    });
    const page = new DOMParser().parseFromString(html, 'text/html');
    expect([...page.querySelectorAll('section > details > summary > h2')].map((h) => h.textContent)).toEqual(
      READING_ORDER,
    );
    READING_ORDER.forEach((title) => expect(page.body.textContent).toContain(BODY_MARKERS[title]));
  });

  it('the plain-text download', () => {
    const text = getTextFileFormattedMeetingDetails(MEETING);
    expectInOrder(
      text,
      READING_ORDER.map((title) => `\n${title}\n\n`),
    );
    READING_ORDER.forEach((title) => expect(text).toContain(BODY_MARKERS[title]));
  });

  it('the email', () => {
    const body = decodeURIComponent(getEmailFormattedSummary(STORED));
    expectInOrder(body, READING_ORDER);
    READING_ORDER.forEach((title) => expect(body).toContain(BODY_MARKERS[title]));
  });

  it('includes the sections the Summary tab shows in their own place (CHAPTERS, AGENDA COVERAGE)', async () => {
    // The tab shows these two beside the summary rather than in its list.
    const tabList = orderSummarySections(summarySections(STORED, ['CHAPTERS', 'AGENDA COVERAGE']));
    expect(tabList.map((section) => section.title)).not.toContain('CHAPTERS');
    const shownByTab = [...tabList.map((section) => section.title), 'CHAPTERS', 'AGENDA COVERAGE'];
    const markdown = summaryDownloadMarkdown(MEETING);
    const blocks = await summaryDocBlocks(MEETING, null, { loadThumbnails: noThumbnails });
    const html = await summaryDownloadHtml(MEETING, null, {
      loadThumbnails: noThumbnails,
      loadRenderer: async () => htmlExport,
    });
    const text = getTextFileFormattedMeetingDetails(MEETING);
    const docHeadings = blocks.filter((block) => block.kind === 'heading').map((block) => block.runs[0].text);
    shownByTab.forEach((title) => {
      expect(markdown).toContain(`## ${title}`);
      expect(docHeadings).toContain(title);
      expect(html).toContain(`<h2>${title.replaceAll('&', '&amp;')}</h2>`);
      expect(text).toContain(`${title}\n\n`);
    });
  });
});

describe("the meeting's facts at the top of the summary downloads", () => {
  const PARTICIPANTS = ['Sarah Lee', 'Bob Chen'];

  it('list the title, date, participants and duration before the first section', async () => {
    const markdown = summaryDownloadMarkdown(MEETING, null, { participants: PARTICIPANTS });
    expectInOrder(markdown, [
      '# Weekly sync',
      '**Date:**',
      '**Participants:** Sarah Lee, Bob Chen',
      '**Duration:** 00:17:16',
      '## SUMMARY',
    ]);
    const text = getTextFileFormattedMeetingDetails(MEETING, null, { participants: PARTICIPANTS });
    expectInOrder(text, ['Title:', 'Date:', 'Participants: Sarah Lee, Bob Chen', 'Length: 00:17:16', 'SUMMARY']);
    const html = await summaryDownloadHtml(MEETING, null, {
      participants: PARTICIPANTS,
      loadThumbnails: noThumbnails,
      loadRenderer: async () => htmlExport,
    });
    expectInOrder(html, ['<h1>Weekly sync', '<dt>Date</dt>', '<dd>Sarah Lee, Bob Chen</dd>', '<dd>00:17:16</dd>']);
  });

  it('leave the participants out when the page has none to give', () => {
    expect(summaryDownloadMarkdown(MEETING)).not.toContain('Participants');
    expect(getTextFileFormattedMeetingDetails(MEETING)).not.toContain('Participants');
  });
});
