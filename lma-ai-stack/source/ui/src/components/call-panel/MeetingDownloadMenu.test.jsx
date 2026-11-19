/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The Download menu's files beyond the summary and the transcript: an archive
 * prepared on the server (Preparing download…, then its link, or the reason it
 * failed), a chat attachment read again for a fresh link, and a recording
 * signed when it is opened.
 */
import React from 'react';
import { afterEach, describe, it, expect, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';

import MeetingDownloadMenu from './MeetingDownloadMenu';
import { prepareAndWaitForArchive } from '../../hooks/use-meeting-archive';
import { DONE_STATUS } from '../common/get-recording-status';

const ITEM = {
  callId: 'Weekly sync-2026-11-03-14-05-09-123',
  callSummaryText: '{"SUMMARY":"ok"}',
  recordingStatusLabel: DONE_STATUS,
  recordingUrl: 'https://rec.s3.us-east-1.amazonaws.com/lma-audio-recordings/a.wav',
  recordingUrls: ['https://rec.s3.us-east-1.amazonaws.com/lma-audio-recordings/first.wav'],
};
const ATTACHMENTS = [{ AttachmentId: 'att1', FileName: 'plan.pdf', Status: 'STORED' }];

const renderMenu = (props = {}) =>
  render(
    <MeetingDownloadMenu
      item={ITEM}
      callTranscriptPerCallId={{}}
      keyframeCount={2}
      loadAttachments={vi.fn(async () => ATTACHMENTS)}
      // eslint-disable-next-line react/jsx-props-no-spreading -- each test's own dependencies
      {...props}
    />,
  );

const openMenu = () => fireEvent.click(screen.getByRole('button', { name: 'Download' }));
const expandGroup = (name) => {
  [...document.body.children].forEach((child) => {
    // eslint-disable-next-line no-param-reassign -- the test's own render container
    child.style.overflow = 'hidden';
  });
  fireEvent.click(screen.getByRole('menuitem', { name }));
};
const choose = (name) => fireEvent.click(screen.getByRole('menuitem', { name }));

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe('Everything (.zip)', () => {
  it('says the download is being prepared, then gives its link', async () => {
    let finish;
    const prepareArchive = vi.fn(
      (_callId, _contents, { onUpdate }) =>
        new Promise((resolve) => {
          onUpdate({ ArchiveId: 'a'.repeat(32), Status: 'PREPARING' });
          finish = () =>
            resolve({
              Status: 'READY',
              FileName: 'Meeting-Weekly_sync.zip',
              DownloadUrl: 'https://signed.example.com/archive.zip',
            });
        }),
    );
    renderMenu({ prepareArchive });
    await waitFor(() => expect(screen.getByRole('button', { name: 'Download' })).toBeTruthy());
    openMenu();
    choose('Everything (.zip)');
    expect(await screen.findByText(/Preparing download… \(Everything\)/)).toBeTruthy();
    expect(prepareArchive).toHaveBeenCalledWith(ITEM.callId, 'EVERYTHING', expect.any(Object));
    finish();
    const link = await screen.findByRole('link', { name: /Download Meeting-Weekly_sync.zip/ });
    expect(link.getAttribute('href')).toBe('https://signed.example.com/archive.zip');
  });

  it('says why an archive could not be prepared', async () => {
    const prepareArchive = vi.fn(async () => ({
      Status: 'FAILED',
      Message: "The meeting's files come to 6.2 GB, more than the 5 GB an archive can hold.",
    }));
    renderMenu({ prepareArchive });
    openMenu();
    choose('Everything (.zip)');
    expect(await screen.findByText(/more than the 5 GB an archive can hold/)).toBeTruthy();
  });

  it('prepares the key frames and the attachments as archives of their own', async () => {
    const prepareArchive = vi.fn(async () => ({ Status: 'READY', DownloadUrl: 'https://s.example.com/x.zip' }));
    renderMenu({ prepareArchive });
    openMenu();
    expandGroup('Screen share');
    choose('Key frames (.zip)');
    await waitFor(() => expect(prepareArchive).toHaveBeenCalledWith(ITEM.callId, 'KEY_FRAMES', expect.any(Object)));
    openMenu();
    expandGroup('Chat attachments');
    await waitFor(() => expect(screen.getByRole('menuitem', { name: 'All attachments (.zip)' })).toBeTruthy());
    choose('All attachments (.zip)');
    await waitFor(() =>
      expect(prepareArchive).toHaveBeenCalledWith(ITEM.callId, 'CHAT_ATTACHMENTS', expect.any(Object)),
    );
  });
});

describe('prepareAndWaitForArchive', () => {
  it('reads the status until the archive is ready', async () => {
    const answers = [
      { data: { prepareMeetingArchive: { ArchiveId: 'x', Status: 'PREPARING' } } },
      { data: { getMeetingArchive: { ArchiveId: 'x', Status: 'PREPARING' } } },
      { data: { getMeetingArchive: { ArchiveId: 'x', Status: 'READY', DownloadUrl: 'u' } } },
    ];
    const request = vi.fn(async () => answers.shift());
    const updates = [];
    const result = await prepareAndWaitForArchive('m', 'EVERYTHING', {
      request,
      sleep: async () => {},
      onUpdate: (update) => updates.push(update.Status),
    });
    expect(result).toMatchObject({ Status: 'READY', DownloadUrl: 'u' });
    expect(updates).toEqual(['PREPARING', 'PREPARING', 'READY']);
    expect(request.mock.calls[1][1]).toEqual({ CallId: 'm', ArchiveId: 'x' });
  });

  it('gives up with a message after the longest wait', async () => {
    const request = vi.fn(async () => ({ data: { prepareMeetingArchive: { ArchiveId: 'x', Status: 'PREPARING' } } }));
    const result = await prepareAndWaitForArchive('m', 'EVERYTHING', {
      request: async (query, variables) =>
        variables.ArchiveId ? { data: { getMeetingArchive: { ArchiveId: 'x', Status: 'PREPARING' } } } : request(),
      sleep: async () => {},
      pollMs: 1000,
      maxWaitMs: 3000,
    });
    expect(result.Status).toBe('FAILED');
    expect(result.Message).toMatch(/too long/);
  });
});

describe('a chat attachment', () => {
  it('is read again for a fresh link, which the new tab follows', async () => {
    const loadAttachments = vi.fn(async (_callId, attachmentId) =>
      attachmentId ? [{ ...ATTACHMENTS[0], DownloadUrl: 'https://signed.example.com/plan.pdf' }] : ATTACHMENTS,
    );
    const openFile = vi.fn(async (signLink) => Boolean(await signLink()));
    renderMenu({ loadAttachments, openFile });
    await waitFor(() => expect(loadAttachments).toHaveBeenCalledWith(ITEM.callId));
    openMenu();
    expandGroup('Chat attachments');
    choose('plan.pdf');
    await waitFor(() => expect(loadAttachments).toHaveBeenCalledWith(ITEM.callId, 'att1'));
    expect(await openFile.mock.results[0].value).toBe(true);
  });
});

describe('a recording', () => {
  it('is signed when it is opened, one entry per session', async () => {
    const sign = vi.fn(async (url) => `${url}?signed`);
    const openFile = vi.fn(async (signLink) => Boolean(await signLink()));
    renderMenu({ sign, openFile });
    openMenu();
    expandGroup('Recordings');
    choose('Audio, session 2');
    // Session 2 is the current recording; session 1 the earlier one the meeting keeps.
    await waitFor(() => expect(sign).toHaveBeenCalledWith(ITEM.recordingUrl, undefined));
  });
});
