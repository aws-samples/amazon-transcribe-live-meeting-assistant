/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React from 'react';
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';

import AgendaPanel, { deriveStatus } from './AgendaPanel';

const ITEM = {
  callId: 'c1',
  agenda: '1. Introductions\n2. Budget\n\n3. Hiring',
  agendaSource: 'scheduled',
  agendaUpdatedBy: 'alice',
  agendaUpdatedAt: '2026-10-31T10:00:00.000Z',
};

const textareaOf = (container) => container.querySelector('textarea');

describe('AgendaPanel', () => {
  it('shows the stored agenda as a numbered list with its source', () => {
    const { container } = render(<AgendaPanel item={ITEM} onSave={vi.fn()} readOnly={false} />);
    const list = container.querySelector('ol');
    expect(
      within(list)
        .getAllByRole('listitem')
        .map((li) => li.textContent),
    ).toEqual(['Introductions', 'Budget', 'Hiring']);
    expect(screen.getByText('From scheduling')).toBeTruthy();
    expect(screen.getByText(/Updated by alice/)).toBeTruthy();
  });

  it('says so when the meeting has no agenda, and offers to add one', () => {
    render(<AgendaPanel item={{ callId: 'c1' }} onSave={vi.fn()} readOnly={false} />);
    expect(screen.getByText('No agenda for this meeting.')).toBeTruthy();
    expect(screen.getByRole('button', { name: /Add agenda/ })).toBeTruthy();
  });

  it('edits starting from the stored text and saves the trimmed draft', async () => {
    const onSave = vi.fn().mockResolvedValue(true);
    const { container } = render(<AgendaPanel item={ITEM} onSave={onSave} readOnly={false} />);
    fireEvent.click(screen.getByRole('button', { name: /Edit/ }));
    const textarea = textareaOf(container);
    expect(textarea.value).toBe(ITEM.agenda);
    fireEvent.change(textarea, { target: { value: '  Budget\nHiring  ' } });
    fireEvent.click(screen.getByRole('button', { name: /Save agenda/ }));
    await waitFor(() => expect(onSave).toHaveBeenCalledWith('Budget\nHiring'));
    await waitFor(() => expect(textareaOf(container)).toBeNull());
  });

  it('stays in edit mode when the write is refused, so nothing typed is lost', async () => {
    const onSave = vi.fn().mockResolvedValue(false);
    const { container } = render(<AgendaPanel item={ITEM} onSave={onSave} readOnly={false} />);
    fireEvent.click(screen.getByRole('button', { name: /Edit/ }));
    fireEvent.change(textareaOf(container), { target: { value: 'Budget' } });
    fireEvent.click(screen.getByRole('button', { name: /Save agenda/ }));
    await waitFor(() => expect(onSave).toHaveBeenCalled());
    expect(textareaOf(container).value).toBe('Budget');
  });

  it('offers no edit control to a viewer without write access', () => {
    render(<AgendaPanel item={ITEM} onSave={vi.fn()} readOnly />);
    expect(screen.queryByRole('button', { name: /Edit/ })).toBeNull();
  });

  it('offers no edit control when the context supplies no writer', () => {
    render(<AgendaPanel item={ITEM} readOnly={false} />);
    expect(screen.queryByRole('button', { name: /Edit/ })).toBeNull();
  });

  it('derives straight away when there is no agenda, and says what it found', async () => {
    const onDerive = vi.fn().mockResolvedValue({ Success: true, Derived: false, Message: 'no agenda was stated' });
    render(<AgendaPanel item={{ callId: 'c1' }} onSave={vi.fn()} onDerive={onDerive} readOnly={false} />);
    fireEvent.click(screen.getByRole('button', { name: /Derive agenda/ }));
    await waitFor(() => expect(onDerive).toHaveBeenCalledTimes(1));
    expect(await screen.findByText('no agenda was stated')).toBeTruthy();
  });

  it('asks before replacing an agenda somebody gave, and keeps it on "Keep it"', async () => {
    const onDerive = vi.fn().mockResolvedValue({ Success: true, Derived: true });
    render(<AgendaPanel item={ITEM} onSave={vi.fn()} onDerive={onDerive} readOnly={false} />);
    fireEvent.click(screen.getByRole('button', { name: /Derive agenda/ }));
    expect(onDerive).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: /Keep it/ }));
    expect(onDerive).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: /Derive agenda/ }));
    fireEvent.click(screen.getByRole('button', { name: /^Replace$/ }));
    await waitFor(() => expect(onDerive).toHaveBeenCalledTimes(1));
  });

  it('re-derives a derived agenda without asking', async () => {
    const onDerive = vi.fn().mockResolvedValue({ Success: true, Derived: true });
    render(
      <AgendaPanel item={{ ...ITEM, agendaSource: 'derived' }} onSave={vi.fn()} onDerive={onDerive} readOnly={false} />,
    );
    fireEvent.click(screen.getByRole('button', { name: /Derive agenda/ }));
    await waitFor(() => expect(onDerive).toHaveBeenCalledTimes(1));
  });

  it('offers no Derive control to a viewer without write access', () => {
    render(<AgendaPanel item={ITEM} onSave={vi.fn()} onDerive={vi.fn()} readOnly />);
    expect(screen.queryByRole('button', { name: /Derive agenda/ })).toBeNull();
  });

  it('describes each outcome of a derivation', () => {
    expect(deriveStatus({ Success: true, Derived: true }).type).toBe('success');
    expect(deriveStatus({ Success: true, Derived: false, Message: 'none' })).toEqual({ type: 'info', text: 'none' });
    expect(deriveStatus({ Success: false, Message: 'not permitted' })).toEqual({
      type: 'error',
      text: 'not permitted',
    });
    expect(deriveStatus(undefined).type).toBe('error');
    expect(deriveStatus({ Success: false, NoAnswer: true })).toEqual({
      type: 'info',
      text: 'Still reading the transcript; a stated agenda will appear here when found',
    });
  });
});
