/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Download › Whiteboards (PDF), Meeting pack… and Summary › Email
 * participants…, through the Download menu: what is ticked, what is disabled
 * and why, progress and sizes, the size warning, the .eml draft, and the
 * mailto fallback.
 */
import React from 'react';
import { afterEach, describe, it, expect, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';

import MeetingDownloadMenu from './MeetingDownloadMenu';
import { DONE_STATUS, IN_PROGRESS_STATUS } from '../common/get-recording-status';

const ITEM = {
  callId: 'Weekly sync-2026-11-03-14-05-09-123',
  callSummaryText: '{"SUMMARY":"Agreed the plan."}',
  recordingStatusLabel: DONE_STATUS,
  participants: [
    { name: 'Ana Lima', email: 'ana@example.com' },
    { name: 'Bo', email: 'bo@example.org' },
  ],
};
const BOARDS = [{ WhiteboardId: 'w1', Title: 'Risks', Version: 1, Panels: [] }];
const MB = 1024 * 1024;

const pdf = (bytes) => new Blob([new Uint8Array(bytes)], { type: 'application/pdf' });
const builders = (sizes = {}) => ({
  summary: vi.fn(async () => pdf(sizes.summary ?? 1000)),
  slides: vi.fn(async (_ctx, { onProgress }) => {
    onProgress(1, 2);
    return pdf(sizes.slides ?? 2000);
  }),
  whiteboards: vi.fn(async () => pdf(sizes.whiteboards ?? 3000)),
});

const renderMenu = (props = {}) =>
  render(
    <MeetingDownloadMenu
      item={ITEM}
      callTranscriptPerCallId={{}}
      keyframeCount={2}
      whiteboards={BOARDS}
      loadAttachments={vi.fn(async () => [])}
      packBuilders={builders()}
      saveFile={vi.fn()}
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
const dialog = () => screen.getByRole('dialog');
const part = (id) => screen.getByTestId(`pack-part-${id}`);
const checkbox = (id) => within(part(id)).getByRole('checkbox');

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe('Meeting pack', () => {
  it('ticks every part the meeting has, builds each, and shows its size', async () => {
    renderMenu();
    openMenu();
    choose('Meeting pack (PDF files)…');
    expect(within(dialog()).getByText('Meeting pack')).toBeTruthy();
    ['summary', 'slides', 'whiteboards'].forEach((id) => expect(checkbox(id).checked).toBe(true));
    await waitFor(() => expect(within(part('whiteboards')).getByText('3 KB')).toBeTruthy());
    expect(within(part('summary')).getByText('1000 B')).toBeTruthy();
    expect(screen.getByTestId('pack-total').textContent).toBe('Total: 6 KB');
  });

  it('cannot tick what the meeting does not have, and says why', async () => {
    renderMenu({ keyframeCount: 0, whiteboards: [], whiteboardsWritable: false });
    openMenu();
    choose('Meeting pack (PDF files)…');
    expect(checkbox('slides').checked).toBe(false);
    expect(checkbox('slides').disabled).toBe(true);
    expect(within(part('slides')).getByText('Nothing was shared on screen in this meeting.')).toBeTruthy();
    expect(within(part('whiteboards')).getByText(/transcript was deleted, and its whiteboards with it/)).toBeTruthy();
    expect(checkbox('summary').checked).toBe(true);
  });

  it('saves the chosen parts as one .zip, or as separate files', async () => {
    const saveFile = vi.fn();
    renderMenu({ saveFile });
    openMenu();
    choose('Meeting pack (PDF files)…');
    fireEvent.click(checkbox('slides'));
    const download = within(dialog()).getByRole('button', { name: 'Download' });
    await waitFor(() => expect(download.disabled).toBe(false));
    fireEvent.click(download);
    await waitFor(() => expect(saveFile).toHaveBeenCalledTimes(1));
    expect(saveFile.mock.calls[0][1]).toMatch(/^Meeting-pack-Weekly sync-.*\.zip$/);
    fireEvent.click(screen.getByLabelText('Separate files'));
    fireEvent.click(download);
    await waitFor(() => expect(saveFile).toHaveBeenCalledTimes(3), { timeout: 3000 });
    expect(saveFile.mock.calls.slice(1).map((call) => call[1].split('-')[0])).toEqual(['Summary', 'Whiteboards']);
  });

  it('says the files are a snapshot while the meeting is going on', () => {
    renderMenu({ item: { ...ITEM, recordingStatusLabel: IN_PROGRESS_STATUS } });
    openMenu();
    choose('Meeting pack (PDF files)…');
    expect(within(dialog()).getByText(/still going on: the files are a snapshot/)).toBeTruthy();
  });

  it('offers a part that could not be built again, and holds the download until then', async () => {
    const failing = builders();
    failing.slides.mockRejectedValueOnce(new Error('network'));
    vi.spyOn(console, 'error').mockImplementation(() => {});
    renderMenu({ packBuilders: failing });
    openMenu();
    choose('Meeting pack (PDF files)…');
    await waitFor(() => expect(within(part('slides')).getByText('This file could not be prepared.')).toBeTruthy());
    expect(within(dialog()).getByRole('button', { name: 'Download' }).disabled).toBe(true);
    fireEvent.click(within(part('slides')).getByRole('button', { name: 'Try again' }));
    await waitFor(() => expect(within(part('slides')).getByText('2 KB')).toBeTruthy());
    expect(failing.slides).toHaveBeenCalledTimes(2);
  });
});

describe('Whiteboards (PDF)', () => {
  it('saves the whiteboards PDF, built in one step', async () => {
    const saveFile = vi.fn();
    renderMenu({ saveFile });
    openMenu();
    choose('Whiteboards (PDF)');
    await waitFor(() => expect(saveFile).toHaveBeenCalledTimes(1));
    expect(saveFile.mock.calls[0][1]).toBe('Whiteboards-Weekly sync-2026-11-03-14-05-09-123.pdf');
  });

  it('is disabled, with the reason, for a meeting without whiteboards', () => {
    renderMenu({ whiteboards: [] });
    openMenu();
    expect(screen.getByRole('menuitem', { name: /Whiteboards \(PDF\)/ }).getAttribute('aria-disabled')).toBe('true');
  });
});

describe('Email participants', () => {
  const openEmail = (props) => {
    renderMenu(props);
    openMenu();
    expandGroup('Summary');
    choose('Summary: Email participants…');
  };

  it("lists the meeting's participants and ticks every file to attach", async () => {
    openEmail();
    expect(within(dialog()).getByText('Ana Lima')).toBeTruthy();
    expect(within(dialog()).getByText('bo@example.org')).toBeTruthy();
    ['summary', 'slides', 'whiteboards'].forEach((id) => expect(checkbox(id).checked).toBe(true));
    await waitFor(() =>
      expect(screen.getByTestId('pack-total').textContent).toMatch(/^Total: 6 KB \(about 8 KB as an email\)$/),
    );
    expect(within(dialog()).queryByText('The email may be too large to send')).toBeNull();
  });

  it('warns when the email would be too large, and the warning goes once the slides are unticked', async () => {
    openEmail({ packBuilders: builders({ slides: 18 * MB }) });
    await waitFor(() => expect(within(dialog()).getByText('The email may be too large to send')).toBeTruthy());
    fireEvent.click(checkbox('slides'));
    await waitFor(() => expect(within(dialog()).queryByText('The email may be too large to send')).toBeNull());
  });

  it('saves an .eml draft addressed to the participants, with the chosen files attached', async () => {
    const saveFile = vi.fn();
    openEmail({ saveFile });
    fireEvent.click(checkbox('slides'));
    const create = within(dialog()).getByRole('button', { name: 'Create email draft (.eml)' });
    await waitFor(() => expect(create.getAttribute('aria-disabled') ?? (create.disabled ? 'true' : null)).toBeNull());
    fireEvent.click(create);
    await waitFor(() => expect(saveFile).toHaveBeenCalledTimes(1));
    const [blob, name] = saveFile.mock.calls[0];
    expect(name).toMatch(/^Email-Weekly sync-.*\.eml$/);
    expect(blob.type).toBe('message/rfc822');
    const message = await blob.text();
    expect(message).toContain('X-Unsent: 1');
    expect(message).toContain('To: "Ana Lima" <ana@example.com>, "Bo" <bo@example.org>');
    expect(message).toContain('Subject: Weekly sync summary');
    expect(message.match(/Content-Type: application\/pdf/g)).toHaveLength(2);
    expect(message).not.toContain('Slides-');
    expect(await screen.findByText(/Open it in your mail app/)).toBeTruthy();
  });

  it('leaves out a recipient taken off the list', async () => {
    const saveFile = vi.fn();
    openEmail({ saveFile });
    fireEvent.click(within(dialog()).getAllByRole('button', { name: /Remove/ })[0]);
    expect(within(dialog()).queryByText('Ana Lima')).toBeNull();
    await waitFor(() => expect(within(part('whiteboards')).getByText('3 KB')).toBeTruthy());
    fireEvent.click(within(dialog()).getByRole('button', { name: 'Create email draft (.eml)' }));
    await waitFor(() => expect(saveFile).toHaveBeenCalledTimes(1));
    expect(await saveFile.mock.calls[0][0].text()).toContain('To: "Bo" <bo@example.org>\r\n');
  });

  it('says so when no participant has an address', () => {
    openEmail({ item: { ...ITEM, participants: [{ name: 'No address', email: '' }] } });
    expect(within(dialog()).getByText(/no recipients/)).toBeTruthy();
  });

  it('opens the mail app with the summary and no attachments, and offers the files to attach by hand', async () => {
    const open = vi.spyOn(window, 'open').mockImplementation(() => null);
    const saveFile = vi.fn();
    openEmail({ saveFile });
    fireEvent.click(within(dialog()).getByRole('button', { name: 'Open in mail app (no attachments)' }));
    expect(open.mock.calls[0][0]).toMatch(
      /^mailto:ana@example.com,bo@example.org\?subject=Weekly%20sync%20summary&body=/,
    );
    expect(within(dialog()).getByText(/cannot carry attachments/)).toBeTruthy();
    await waitFor(() => expect(within(part('whiteboards')).getByText('3 KB')).toBeTruthy());
    fireEvent.click(within(dialog()).getByRole('button', { name: 'Download the files' }));
    await waitFor(() => expect(saveFile).toHaveBeenCalledTimes(3), { timeout: 3000 });
  });
});
