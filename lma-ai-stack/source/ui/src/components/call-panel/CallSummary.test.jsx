/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * What the Meeting Summary panel must get right.
 *
 * Regenerating is not a request that answers with a summary: summarising calls a
 * model once per prompt template and outlives a GraphQL request, so the mutation
 * starts a run and returns, and the new text arrives later through the same
 * subscription an end-of-meeting summary does. Everything worth pinning here
 * follows from that -- the panel has to show progress it did not get from the
 * mutation, must not start a second run while one is going, must say so when a
 * run is refused, and must leave the previous summary on screen when one fails.
 *
 * The hand-edit path is simpler but has one contract of its own: it stores the
 * text as rendered, so that what is saved is what every other reader -- the
 * download, the email, the embedded panel -- will show.
 */
import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';

import { CallSummary } from './CallPanel';
import { SOURCE_MANUAL, STATUS_FAILED, STATUS_RUNNING } from '../common/summary-state';
import { DONE_STATUS, IN_PROGRESS_STATUS } from '../common/get-recording-status';

/* eslint-disable react/prop-types */

const GENERATED = JSON.stringify({ Summary: 'Sarah Lee walked through the migration plan.' });

const ITEM = {
  callId: 'c1',
  callSummaryText: GENERATED,
  callSummaryState: undefined,
  initiationTimeStamp: '2026-03-04T12:00:00.000Z',
  conversationDurationTimeStamp: '00:12:00',
};

let onSave;

let onRegenerate;

const renderPanel = ({ item = ITEM, readOnly = false, regenerateResult = { Success: true }, saved = true } = {}) => {
  onSave = vi.fn().mockResolvedValue(saved);
  onRegenerate = vi.fn().mockResolvedValue(regenerateResult);
  return render(<CallSummary item={item} onSave={onSave} onRegenerate={onRegenerate} readOnly={readOnly} />);
};

const withState = (state, text = GENERATED) => ({
  ...ITEM,
  callSummaryText: text,
  callSummaryState: JSON.stringify(state),
});

beforeEach(() => {
  vi.clearAllMocks();
});

describe('CallSummary — regenerating', () => {
  it('asks the backend for a run when Regenerate is pressed', async () => {
    renderPanel();
    fireEvent.click(screen.getByRole('button', { name: /regenerate/i }));
    await waitFor(() => expect(onRegenerate).toHaveBeenCalledTimes(1));
  });

  it('shows progress straight away, before the backend has marked anything', async () => {
    // The whole point: the mutation returns before the run finishes, so a panel
    // that waited for the result would look like it had done nothing.
    renderPanel();
    fireEvent.click(screen.getByRole('button', { name: /regenerate/i }));
    await waitFor(() => expect(screen.getByText(/regenerating/i)).toBeInTheDocument());
  });

  it('shows progress for a run somebody else started', () => {
    // A second tab, or another user, learns about the run from the meeting.
    renderPanel({ item: withState({ status: STATUS_RUNNING, startedAt: new Date().toISOString() }) });
    expect(screen.getByText(/regenerating/i)).toBeInTheDocument();
  });

  it('will not start a second run while one is already going', () => {
    renderPanel({ item: withState({ status: STATUS_RUNNING, startedAt: new Date().toISOString() }) });
    fireEvent.click(screen.getByRole('button', { name: /regenerate/i }));
    expect(onRegenerate).not.toHaveBeenCalled();
  });

  it('allows a run again once a stale mark has expired', async () => {
    renderPanel({
      item: withState({ status: STATUS_RUNNING, startedAt: new Date(Date.now() - 86400000).toISOString() }),
    });
    fireEvent.click(screen.getByRole('button', { name: /regenerate/i }));
    await waitFor(() => expect(onRegenerate).toHaveBeenCalledTimes(1));
  });

  it('says why when the backend refuses the run', async () => {
    renderPanel({ regenerateResult: { Success: false, Message: 'not permitted' } });
    fireEvent.click(screen.getByRole('button', { name: /regenerate/i }));
    await waitFor(() => expect(screen.getByText('not permitted')).toBeInTheDocument());
  });

  it('stops showing progress when the run is refused', async () => {
    renderPanel({ regenerateResult: { Success: false, Message: 'a summary is already being regenerated' } });
    fireEvent.click(screen.getByRole('button', { name: /regenerate/i }));
    await waitFor(() => expect(screen.getByText(/already being regenerated/i)).toBeInTheDocument());
    expect(screen.queryByText(/^Regenerating$/)).not.toBeInTheDocument();
  });

  it('drops a refusal once a new summary arrives', async () => {
    // The usual refusal is "a run is already going", raised against a run in
    // another tab. It is hidden while that run is in flight, so without this it
    // would come back beside that run's result when it landed.
    const { rerender } = renderPanel({
      regenerateResult: { Success: false, Message: 'a summary is already being regenerated' },
    });
    fireEvent.click(screen.getByRole('button', { name: /regenerate/i }));
    await waitFor(() => expect(screen.getByText(/already being regenerated/i)).toBeInTheDocument());

    const arrived = { ...ITEM, callSummaryText: JSON.stringify({ Summary: 'Sarah Lee owns the migration.' }) };
    rerender(<CallSummary item={arrived} onSave={onSave} onRegenerate={onRegenerate} readOnly={false} />);
    await waitFor(() => expect(screen.queryByText(/already being regenerated/i)).not.toBeInTheDocument());
    expect(screen.getByText(/owns the migration/)).toBeInTheDocument();
  });

  it('surfaces a failed run and keeps the previous summary on screen', () => {
    renderPanel({ item: withState({ status: STATUS_FAILED, message: 'could not regenerate the summary just now' }) });
    expect(screen.getByText(/could not regenerate the summary just now/i)).toBeInTheDocument();
    expect(screen.getByText(/migration plan/)).toBeInTheDocument();
  });

  it('disables both controls for a user who may not edit the meeting', () => {
    renderPanel({ readOnly: true });
    expect(screen.getByRole('button', { name: /regenerate/i })).toBeDisabled();
    expect(screen.getByRole('button', { name: /^edit$/i })).toBeDisabled();
  });
});

describe('CallSummary — replacing a hand-edited summary', () => {
  it('asks before replacing text somebody wrote', () => {
    renderPanel({ item: withState({ source: SOURCE_MANUAL }) });
    fireEvent.click(screen.getByRole('button', { name: /regenerate/i }));
    expect(onRegenerate).not.toHaveBeenCalled();
    expect(screen.getByText(/replace the edited summary/i)).toBeInTheDocument();
  });

  it('starts the run once the replacement is confirmed', async () => {
    renderPanel({ item: withState({ source: SOURCE_MANUAL }) });
    fireEvent.click(screen.getByRole('button', { name: /regenerate/i }));
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: /regenerate/i }));
    await waitFor(() => expect(onRegenerate).toHaveBeenCalledTimes(1));
  });

  it('does not ask before replacing a generated summary', async () => {
    renderPanel();
    fireEvent.click(screen.getByRole('button', { name: /regenerate/i }));
    expect(screen.queryByText(/replace the edited summary/i)).not.toBeInTheDocument();
    await waitFor(() => expect(onRegenerate).toHaveBeenCalledTimes(1));
  });

  it('marks a hand-edited summary as edited', () => {
    renderPanel({ item: withState({ source: SOURCE_MANUAL }) });
    expect(screen.getByText('Edited')).toBeInTheDocument();
  });

  it('does not mark a generated summary as edited', () => {
    renderPanel();
    expect(screen.queryByText('Edited')).not.toBeInTheDocument();
  });
});

describe('CallSummary — editing by hand', () => {
  it('seeds the editor with the summary as rendered, not the stored JSON', () => {
    // Asking somebody to edit a JSON object to fix a sentence would be absurd,
    // and what is saved has to be what every other reader will show.
    renderPanel();
    fireEvent.click(screen.getByRole('button', { name: /^edit$/i }));
    const textarea = screen.getByLabelText('Meeting summary text');
    expect(textarea.value).toContain('Sarah Lee walked through the migration plan.');
    expect(textarea.value).not.toContain('{"Summary"');
  });

  it('saves the edited text', async () => {
    renderPanel();
    fireEvent.click(screen.getByRole('button', { name: /^edit$/i }));
    fireEvent.change(screen.getByLabelText('Meeting summary text'), {
      target: { value: 'Sarah Lee owns the migration.' },
    });
    fireEvent.click(screen.getByRole('button', { name: /save summary/i }));
    await waitFor(() => expect(onSave).toHaveBeenCalledWith('Sarah Lee owns the migration.'));
  });

  it('leaves the editor open when the save was rejected', async () => {
    // Closing it would discard text that was never stored.
    renderPanel({ saved: false });
    fireEvent.click(screen.getByRole('button', { name: /^edit$/i }));
    fireEvent.click(screen.getByRole('button', { name: /save summary/i }));
    await waitFor(() => expect(onSave).toHaveBeenCalled());
    expect(screen.getByLabelText('Meeting summary text')).toBeInTheDocument();
  });

  it('discards the draft on cancel', () => {
    renderPanel();
    fireEvent.click(screen.getByRole('button', { name: /^edit$/i }));
    fireEvent.change(screen.getByLabelText('Meeting summary text'), { target: { value: 'nonsense' } });
    fireEvent.click(screen.getByRole('button', { name: /cancel/i }));
    expect(onSave).not.toHaveBeenCalled();
    expect(screen.getByText(/migration plan/)).toBeInTheDocument();
  });

  it('cannot be opened while a run is going', () => {
    renderPanel({ item: withState({ status: STATUS_RUNNING, startedAt: new Date().toISOString() }) });
    expect(screen.getByRole('button', { name: /^edit$/i })).toBeDisabled();
  });

  it('cannot be opened on a meeting with no summary yet', () => {
    renderPanel({ item: { ...ITEM, callSummaryText: undefined } });
    expect(screen.getByRole('button', { name: /^edit$/i })).toBeDisabled();
  });

  it('cannot be opened while the meeting is still running', () => {
    // The end-of-meeting summary replaces the stored text when the meeting
    // ends, with none of the confirmation an edit is promised, so editing waits
    // for the meeting to finish. Regenerating does not: what it produces is a
    // generated summary either way.
    renderPanel({ item: { ...ITEM, recordingStatusLabel: IN_PROGRESS_STATUS } });
    // `aria-disabled` rather than the `disabled` attribute the other cases use:
    // this one carries a disabledReason, and Cloudscape keeps a button with one
    // focusable so its explanation can be read.
    const edit = screen.getByRole('button', { name: /^edit$/i });
    expect(edit).toHaveAttribute('aria-disabled', 'true');
    fireEvent.click(edit);
    expect(screen.queryByLabelText('Meeting summary text')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: /regenerate/i })).toBeEnabled();
  });

  it('can be opened once the meeting has ended', () => {
    renderPanel({ item: { ...ITEM, recordingStatusLabel: DONE_STATUS } });
    expect(screen.getByRole('button', { name: /^edit$/i })).toBeEnabled();
  });

  it('will not store a blank summary', () => {
    // An empty string stores as present and reads as no summary, which disables
    // Edit -- leaving Regenerate as the only way back.
    renderPanel();
    fireEvent.click(screen.getByRole('button', { name: /^edit$/i }));
    fireEvent.change(screen.getByLabelText('Meeting summary text'), { target: { value: '   \n ' } });
    expect(screen.getByRole('button', { name: /save summary/i })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: /save summary/i }));
    expect(onSave).not.toHaveBeenCalled();
  });
});
