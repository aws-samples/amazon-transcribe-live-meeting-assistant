/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The meeting pack without React: which parts a meeting has and why not,
 * what is ticked by default, the recipients, the mailto fallback, the size
 * warning, file names, building a part, saving the pack, and the email draft.
 */
import { afterEach, describe, it, expect, vi } from 'vitest';

import {
  EMAIL_SIZE_LIMIT_BYTES,
  NO_SLIDES,
  NO_WHITEBOARDS,
  PartUnavailable,
  WHITEBOARDS_DELETED,
  buildEmailDraft,
  buildPart,
  defaultSelection,
  emailRecipients,
  emailSubject,
  emailTooLarge,
  formatBytes,
  mailtoUrl,
  packPartStates,
  partFileName,
  safeFileName,
  savePack,
} from './meeting-pack';
import { IN_PROGRESS_STATUS } from '../common/get-recording-status';

const ITEM = {
  callId: 'Weekly sync-2026-11-03-14-05-09-123',
  callSummaryText: JSON.stringify({ SUMMARY: 'Agreed the plan.', ACTIONS: '- Ana: ship it' }),
  initiationTimeStamp: '2026-11-03T14:05:09.123Z',
  conversationDurationTimeStamp: '00:17:16',
};

afterEach(() => vi.restoreAllMocks());

describe('the parts a meeting has', () => {
  it('ticks every part the meeting has', () => {
    const states = packPartStates({ hasSummary: true, keyframeCount: 3, whiteboardCount: 2 });
    expect(states.map((part) => [part.id, part.available])).toEqual([
      ['summary', true],
      ['slides', true],
      ['whiteboards', true],
    ]);
    expect(defaultSelection(states)).toEqual({ summary: true, slides: true, whiteboards: true });
  });

  it('leaves out, with the reason, what the meeting does not have', () => {
    const states = packPartStates({ hasSummary: false, summaryReason: 'Summarizing…' });
    expect(states.map((part) => part.reason)).toEqual(['Summarizing…', NO_SLIDES, NO_WHITEBOARDS]);
    expect(defaultSelection(states)).toEqual({ summary: false, slides: false, whiteboards: false });
  });

  it('says the whiteboards went with the transcript when it was deleted', () => {
    const [, , whiteboards] = packPartStates({ hasSummary: true, whiteboardsDeleted: true });
    expect(whiteboards).toMatchObject({ available: false, reason: WHITEBOARDS_DELETED });
  });
});

describe('the email', () => {
  it("is addressed to the meeting's participants, each address once, skipping entries without one", () => {
    expect(
      emailRecipients([
        { name: 'Ana Lima', email: 'ana@example.com' },
        { name: '', email: 'bob+lma@example.com' },
        { name: 'Ana again', email: 'ANA@example.com' },
        { name: 'No address', email: '' },
        { name: 'Not an address', email: 'nobody' },
      ]),
    ).toEqual([
      { name: 'Ana Lima', email: 'ana@example.com' },
      { name: '', email: 'bob+lma@example.com' },
    ]);
    expect(emailRecipients(null)).toEqual([]);
  });

  it('is titled with the meeting name and "summary"', () => {
    expect(emailSubject(ITEM.callId)).toBe('Weekly sync summary');
  });

  it('opens in the mail app with the recipients, the subject and the summary as the body', () => {
    const link = mailtoUrl({
      ...ITEM,
      participants: [
        { name: 'Ana', email: 'ana@example.com' },
        { name: '', email: 'bob+lma@example.com' },
      ],
    });
    expect(link.startsWith('mailto:ana@example.com,bob%2Blma@example.com?subject=Weekly%20sync%20summary&body=')).toBe(
      true,
    );
    expect(decodeURIComponent(link.split('&body=')[1])).toContain('Agreed the plan.');
    expect(mailtoUrl(ITEM).startsWith('mailto:?subject=')).toBe(true);
  });

  it('warns above the size mail services commonly take once encoded', () => {
    expect(emailTooLarge(10 * 1024 * 1024)).toBe(false);
    expect(emailTooLarge(16 * 1024 * 1024)).toBe(true);
    expect(emailTooLarge(EMAIL_SIZE_LIMIT_BYTES)).toBe(true);
  });

  it('is a draft with the summary as text and HTML and each chosen file attached', async () => {
    const renderSummaryEmailHtml = vi.fn(() => '<p>html</p>');
    const message = await buildEmailDraft({
      item: { ...ITEM, recordingStatusLabel: IN_PROGRESS_STATUS },
      recipients: [{ name: 'Ana', email: 'ana@example.com' }],
      files: [{ filename: 'Summary-x.pdf', blob: new Blob(['%PDF-1']) }],
      boundary: 'b',
      loadRenderer: async () => ({ renderSummaryEmailHtml }),
    });
    expect(message.startsWith('X-Unsent: 1\r\nTo: "Ana" <ana@example.com>\r\nSubject: Weekly sync summary')).toBe(true);
    expect(message).toContain('filename="Summary-x.pdf"');
    const { sections, attachments, note } = renderSummaryEmailHtml.mock.calls[0][0];
    expect(sections.map((section) => section.title)).toEqual(['SUMMARY', 'ACTIONS']);
    expect(attachments).toEqual(['Summary-x.pdf']);
    expect(note).toMatch(/still going on/);
  });
});

describe('files', () => {
  it('are named for the meeting, safe to save on every system', () => {
    expect(partFileName('slides', 'Plan: Q1/Q2 - 2026-11-03-14:05:09.123')).toBe(
      'Slides-Plan- Q1-Q2 - 2026-11-03-14-05-09.123.pdf',
    );
    expect(safeFileName('  ..  ')).toBe('meeting');
  });

  it('give their sizes in words', () => {
    expect(formatBytes(512)).toBe('512 B');
    expect(formatBytes(850 * 1024)).toBe('850 KB');
    expect(formatBytes(12.4 * 1024 * 1024)).toBe('12 MB');
    expect(formatBytes(2.5 * 1024 * 1024)).toBe('2.5 MB');
  });

  it('are built by their part, with progress, and say when a part has nothing in it', async () => {
    const onProgress = vi.fn();
    const builders = {
      slides: async (_ctx, { onProgress: progress }) => {
        progress(1, 2);
        return new Blob(['%PDF-abc']);
      },
      whiteboards: async () => {
        throw new PartUnavailable('none');
      },
    };
    const built = await buildPart('slides', { item: ITEM }, { builders, onProgress });
    expect(built).toMatchObject({ id: 'slides', size: 8, filename: `Slides-${safeFileName(ITEM.callId)}.pdf` });
    expect(onProgress).toHaveBeenCalledWith(1, 2);
    await expect(buildPart('whiteboards', { item: ITEM }, { builders })).rejects.toBeInstanceOf(PartUnavailable);
  });

  it('are saved as one .zip, or one after another', async () => {
    const save = vi.fn();
    const files = [
      { filename: 'Summary-x.pdf', blob: new Blob(['a']) },
      { filename: 'Slides-x.pdf', blob: new Blob(['b']) },
    ];
    await savePack(files, { callId: 'x', save });
    expect(save).toHaveBeenCalledTimes(1);
    expect(save.mock.calls[0][1]).toBe('Meeting-pack-x.zip');
    expect(save.mock.calls[0][0].type).toBe('application/zip');
    save.mockClear();
    await savePack(files, { callId: 'x', asZip: false, save, gapMs: 0 });
    expect(save.mock.calls.map((call) => call[1])).toEqual(['Summary-x.pdf', 'Slides-x.pdf']);
  });
});
