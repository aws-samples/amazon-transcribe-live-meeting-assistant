/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The Download menu's entries, and the download each one runs.
 */
import { beforeEach, describe, it, expect, vi } from 'vitest';

vi.mock('../common/download-func', () => ({
  downloadSummaryAsDocx: vi.fn(async () => {}),
  downloadSummaryAsHtml: vi.fn(async () => {}),
  downloadSummaryAsPdf: vi.fn(async () => {}),
  downloadTranscriptAsDocx: vi.fn(async () => {}),
  downloadTranscriptAsExcel: vi.fn(async () => {}),
  downloadTranscriptAsHtml: vi.fn(async () => {}),
  downloadTranscriptAsMarkdown: vi.fn(async () => {}),
  downloadTranscriptAsPdf: vi.fn(async () => {}),
  downloadTranscriptAsText: vi.fn(async () => {}),
  exportToMarkdownFile: vi.fn(async () => {}),
  exportToTextFile: vi.fn(async () => {}),
  summaryDownloadMarkdown: vi.fn(() => '# Summary'),
  transcriptDownloadMarkdown: vi.fn(() => '# Transcript'),
}));

/* eslint-disable import/first -- the module under test is imported after its dependency is mocked */
import * as downloads from '../common/download-func';
import {
  runSummaryAction,
  runTranscriptDownload,
  SUMMARY_ACTION_ITEMS,
  TRANSCRIPT_DOWNLOAD_ITEMS,
} from './meeting-downloads';
/* eslint-enable import/first */

const ITEM = { callId: 'Weekly sync-2026-11-03-14-05-09-123', callSummaryText: '{}' };
const CONSENT = { Mode: 'opt_in' };
const TRANSCRIPT = { [ITEM.callId]: {} };

beforeEach(() => {
  vi.clearAllMocks();
});

describe('the Download menu entries', () => {
  it('offer PDF after Word, and a web page after PDF, for the summary and the transcript', () => {
    expect(SUMMARY_ACTION_ITEMS.map((entry) => entry.id)).toEqual([
      'markdown',
      'docx',
      'pdf',
      'html',
      'text',
      'copy',
      'email',
    ]);
    expect(TRANSCRIPT_DOWNLOAD_ITEMS.map((entry) => entry.id)).toEqual([
      'markdown',
      'docx',
      'pdf',
      'html',
      'text',
      'excel',
      'copy',
    ]);
    expect(SUMMARY_ACTION_ITEMS.find((entry) => entry.id === 'pdf').text).toBe('PDF (.pdf)');
    expect(SUMMARY_ACTION_ITEMS.find((entry) => entry.id === 'html').text).toBe('Web page (.html)');
  });
});

const PARTICIPANTS = { participants: ['Ana Lima', 'Bob Chen'] };
const NO_PARTICIPANTS = { participants: [] };

describe('runSummaryAction', () => {
  it('builds the PDF with the consent record', async () => {
    expect(await runSummaryAction('pdf', ITEM, CONSENT)).toBe(true);
    expect(downloads.downloadSummaryAsPdf).toHaveBeenCalledWith(ITEM, CONSENT, NO_PARTICIPANTS);
    expect(downloads.downloadSummaryAsDocx).not.toHaveBeenCalled();
  });

  it('builds the Word document with its key frames, from the meeting rather than its Markdown', async () => {
    await runSummaryAction('docx', ITEM, CONSENT);
    expect(downloads.downloadSummaryAsDocx).toHaveBeenCalledWith(ITEM, CONSENT, NO_PARTICIPANTS);
    expect(downloads.summaryDownloadMarkdown).not.toHaveBeenCalled();
  });

  it('keeps Markdown as Markdown, with the key frames as text', async () => {
    await runSummaryAction('markdown', ITEM, CONSENT);
    expect(downloads.summaryDownloadMarkdown).toHaveBeenCalledWith(ITEM, CONSENT, NO_PARTICIPANTS);
    expect(downloads.exportToMarkdownFile).toHaveBeenCalledWith('# Summary', `Summary-${ITEM.callId}`);
  });

  it('builds the web page with the consent record and the participants', async () => {
    expect(await runSummaryAction('html', ITEM, CONSENT, PARTICIPANTS)).toBe(true);
    expect(downloads.downloadSummaryAsHtml).toHaveBeenCalledWith(ITEM, CONSENT, PARTICIPANTS);
    expect(downloads.downloadSummaryAsPdf).not.toHaveBeenCalled();
  });

  it('passes the participants to every summary format', async () => {
    await runSummaryAction('markdown', ITEM, CONSENT, PARTICIPANTS);
    await runSummaryAction('docx', ITEM, CONSENT, PARTICIPANTS);
    await runSummaryAction('pdf', ITEM, CONSENT, PARTICIPANTS);
    expect(downloads.summaryDownloadMarkdown).toHaveBeenCalledWith(ITEM, CONSENT, PARTICIPANTS);
    expect(downloads.downloadSummaryAsDocx).toHaveBeenCalledWith(ITEM, CONSENT, PARTICIPANTS);
    expect(downloads.downloadSummaryAsPdf).toHaveBeenCalledWith(ITEM, CONSENT, PARTICIPANTS);
  });
});

describe('runTranscriptDownload', () => {
  it('builds the PDF with the consent record', async () => {
    expect(await runTranscriptDownload('pdf', TRANSCRIPT, ITEM, CONSENT)).toBe(true);
    expect(downloads.downloadTranscriptAsPdf).toHaveBeenCalledWith(TRANSCRIPT, ITEM, CONSENT);
  });

  it('builds the web page with the consent record', async () => {
    expect(await runTranscriptDownload('html', TRANSCRIPT, ITEM, CONSENT)).toBe(true);
    expect(downloads.downloadTranscriptAsHtml).toHaveBeenCalledWith(TRANSCRIPT, ITEM, CONSENT);
  });

  it('leaves the Word transcript as it was', async () => {
    await runTranscriptDownload('docx', TRANSCRIPT, ITEM, CONSENT);
    expect(downloads.downloadTranscriptAsDocx).toHaveBeenCalledWith(TRANSCRIPT, ITEM);
    expect(downloads.downloadTranscriptAsPdf).not.toHaveBeenCalled();
  });
});
