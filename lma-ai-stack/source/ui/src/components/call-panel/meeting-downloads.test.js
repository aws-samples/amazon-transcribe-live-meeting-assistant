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
  downloadMenuItems,
  openSignedFile,
  parseMenuId,
  recordingSessions,
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
  it('offer Word, PDF, the web page, Markdown and text, in that order, for the summary and the transcript', () => {
    expect(SUMMARY_ACTION_ITEMS.map((entry) => entry.id)).toEqual([
      'docx',
      'pdf',
      'html',
      'markdown',
      'text',
      'copy',
      'email',
    ]);
    expect(TRANSCRIPT_DOWNLOAD_ITEMS.map((entry) => entry.id)).toEqual([
      'docx',
      'pdf',
      'html',
      'markdown',
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

describe('downloadMenuItems', () => {
  const BASE = { hasSummary: true, canDownloadTranscript: true };
  const group = (items, text) => items.find((entry) => entry.text === text);
  const ids = (entries) => entries.map((entry) => entry.id);

  it('has a group for each kind of file, and Everything last', () => {
    const items = downloadMenuItems(BASE);
    expect(items.map((entry) => entry.text)).toEqual([
      'Summary',
      'Transcript',
      'Recordings',
      'Screen share',
      'Chat attachments',
      'Everything (.zip)',
    ]);
    expect(items[5]).toMatchObject({ id: 'archive:EVERYTHING' });
    expect(items[5].disabled).toBeFalsy();
  });

  it('disables the transcript, with the reason, once it was deleted', () => {
    const items = downloadMenuItems({
      ...BASE,
      canDownloadTranscript: false,
      transcriptReason: 'The transcript was deleted.',
    });
    group(items, 'Transcript').items.forEach((entry) => {
      expect(entry.disabled).toBe(true);
      expect(entry.disabledReason).toBe('The transcript was deleted.');
    });
  });

  it('offers one recording per session, and says so when there is none', () => {
    const items = downloadMenuItems({ ...BASE, audioUrls: ['a1', 'a2'], videoUrls: [] });
    const recordings = group(items, 'Recordings').items;
    expect(recordings.map((entry) => entry.text)).toEqual(['Audio, session 1', 'Audio, session 2', 'Video']);
    expect(ids(recordings)).toEqual(['recording:audio:0', 'recording:audio:1', 'recording:video:0']);
    expect(recordings[2]).toMatchObject({ disabled: true, disabledReason: 'This meeting has no video recording.' });
    const single = group(downloadMenuItems({ ...BASE, audioUrls: ['a1'] }), 'Recordings').items[0];
    expect(single).toMatchObject({ text: 'Audio' });
    expect(single.disabled).toBeFalsy();
  });

  it('enables the screen share downloads only when the meeting has key frames', () => {
    const none = group(downloadMenuItems(BASE), 'Screen share').items;
    expect(ids(none)).toEqual(['slides:deck', 'archive:KEY_FRAMES']);
    none.forEach((entry) => expect(entry.disabled).toBe(true));
    group(downloadMenuItems({ ...BASE, keyframeCount: 3 }), 'Screen share').items.forEach((entry) =>
      expect(entry.disabled).toBe(false),
    );
  });

  it('lists each stored chat attachment, then all of them as a .zip', () => {
    const attachments = [
      { AttachmentId: 'a1', FileName: 'plan.pdf', Status: 'STORED' },
      { AttachmentId: 'a2', FileName: 'big.mov', Status: 'NOT_STORED' },
      { AttachmentId: 'a3', FileName: 'sketch.png', Status: 'STORED' },
    ];
    const entries = group(downloadMenuItems({ ...BASE, attachments }), 'Chat attachments').items;
    expect(entries.map((entry) => entry.text)).toEqual(['plan.pdf', 'sketch.png', 'All attachments (.zip)']);
    expect(ids(entries)).toEqual(['attachment:a1', 'attachment:a3', 'archive:CHAT_ATTACHMENTS']);
    const none = group(downloadMenuItems(BASE), 'Chat attachments').items;
    expect(none).toHaveLength(1);
    expect(none[0]).toMatchObject({ disabled: true });
  });
});

describe('recordingSessions', () => {
  it('lists every session, the current one last and once', () => {
    expect(recordingSessions('c', ['a', 'b', 'c'])).toEqual(['a', 'b', 'c']);
    expect(recordingSessions('c', ['a'])).toEqual(['a', 'c']);
    expect(recordingSessions('c', undefined)).toEqual(['c']);
    expect(recordingSessions(null, null)).toEqual([]);
  });
});

describe('parseMenuId', () => {
  it('splits at the first colon only', () => {
    expect(parseMenuId('attachment:a:b')).toEqual(['attachment', 'a:b']);
    expect(parseMenuId('archive:EVERYTHING')).toEqual(['archive', 'EVERYTHING']);
    expect(parseMenuId('plain')).toEqual(['plain', '']);
  });
});

describe('openSignedFile', () => {
  it('opens the tab before the link is signed, and points it at the link', async () => {
    const tab = { location: {}, close: vi.fn() };
    const order = [];
    const open = vi.fn(() => {
      order.push('open');
      return tab;
    });
    const ok = await openSignedFile(
      async () => {
        order.push('sign');
        return 'https://signed.example.com/f';
      },
      { open },
    );
    expect(ok).toBe(true);
    expect(order).toEqual(['open', 'sign']);
    expect(tab.location.href).toBe('https://signed.example.com/f');
    expect(tab.opener).toBeNull();
  });

  it('closes the tab when the link cannot be had', async () => {
    const tab = { location: {}, close: vi.fn() };
    expect(await openSignedFile(async () => null, { open: () => tab })).toBe(false);
    expect(tab.close).toHaveBeenCalled();
  });
});
