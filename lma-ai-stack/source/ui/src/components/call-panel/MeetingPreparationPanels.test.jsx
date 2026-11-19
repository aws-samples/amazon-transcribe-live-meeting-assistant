/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
/**
 * The meeting page's preparation panels on their own: the Checklist panel, the
 * Meeting context panel and the labels and audience. Who may edit is decided
 * by the page (readOnly) and enforced by the server; CallPanel.test.jsx checks
 * the owner, a shared user and anyone else on the page itself.
 */
import React from 'react';
import { describe, it, expect, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';

import ChecklistPanel from './ChecklistPanel';
import MeetingContextPanel from './MeetingContextPanel';
import MeetingLabels from './MeetingLabels';

vi.mock('../summary-markdown/SummaryMarkdown', () => ({ default: ({ children }) => <div>{children}</div> }));

const ITEMS = [
  {
    ItemId: 'q1',
    Text: 'Who owns the budget?',
    Kind: 'question',
    Priority: 'must',
    Status: 'covered',
    StatusSource: 'user',
    Evidence: [{ SegmentId: 's1', StartTime: 64, Quote: 'Dana owns it' }],
    Note: 'Dana',
    UpdatedBy: 'alice',
    UpdatedAt: '2026-11-03T14:10:00.000Z',
  },
  { ItemId: 't1', Text: 'Hiring', Kind: 'topic', Status: 'open', Evidence: [] },
];

describe('the Checklist panel', () => {
  it('shows each item with its status, who set it, its note and its evidence', () => {
    const onShowTime = vi.fn();
    render(<ChecklistPanel item={{ callId: 'c', checklist: ITEMS }} readOnly onShowTime={onShowTime} />);
    const first = screen.getByTestId('checklist-item-q1');
    expect(within(first).getByText('Covered')).toBeTruthy();
    expect(within(first).getByText(/Updated by alice/)).toBeTruthy();
    expect(within(first).getByText('Note: Dana')).toBeTruthy();
    expect(within(first).getByText(/"Dana owns it"/)).toBeTruthy();
    fireEvent.click(within(first).getByRole('button', { name: 'Show the transcript at 1:04' }));
    expect(onShowTime).toHaveBeenCalledWith(64);
    expect(screen.getByText('1 open, 1 covered')).toBeTruthy();
  });

  it('says a status came from the assistant', () => {
    const items = [{ ...ITEMS[0], StatusSource: 'assistant', UpdatedBy: 'LMA' }];
    render(<ChecklistPanel item={{ callId: 'c', checklist: items }} readOnly />);
    expect(screen.getByText(/status from the assistant/)).toBeTruthy();
  });

  it('offers no editing when read-only, and is hidden when there is nothing to show', () => {
    const { unmount } = render(
      <ChecklistPanel item={{ callId: 'c', checklist: ITEMS }} readOnly onSaveList={vi.fn()} onUpdateItem={vi.fn()} />,
    );
    expect(screen.queryByRole('button', { name: 'Edit list' })).toBeNull();
    expect(screen.queryByRole('button', { name: /note/ })).toBeNull();
    unmount();
    render(
      <ChecklistPanel item={{ callId: 'c', checklist: [] }} readOnly onSaveList={vi.fn()} onUpdateItem={vi.fn()} />,
    );
    expect(screen.queryByTestId('checklist-panel')).toBeNull();
  });

  it('edits the list: adds, changes, reorders and removes items, then replaces it', async () => {
    const onSaveList = vi.fn(async () => ({ ok: true }));
    render(<ChecklistPanel item={{ callId: 'c', checklist: ITEMS }} onSaveList={onSaveList} onUpdateItem={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: 'Edit list' }));
    fireEvent.click(screen.getByRole('button', { name: 'Add item' }));
    const input = screen.getByRole('textbox', { name: 'Item 3 text' });
    fireEvent.change(input, { target: { value: 'Confirm the date' } });
    fireEvent.click(screen.getByRole('button', { name: 'Move item 3 up' }));
    fireEvent.click(screen.getByRole('button', { name: 'Remove item 1' }));
    fireEvent.click(screen.getByRole('button', { name: 'Save checklist' }));
    await waitFor(() => expect(onSaveList).toHaveBeenCalled());
    const [definitions, reset] = onSaveList.mock.calls[0];
    expect(definitions).toEqual([
      { ItemId: 'item-3', Text: 'Confirm the date', Kind: 'topic', Priority: 'should' },
      { ItemId: 't1', Text: 'Hiring', Kind: 'topic', Priority: 'should' },
    ]);
    expect(reset).toBe(false);
  });

  it('does not offer to save an item without text', () => {
    render(<ChecklistPanel item={{ callId: 'c', checklist: [] }} onSaveList={vi.fn()} onUpdateItem={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: 'Add checklist' }));
    fireEvent.click(screen.getByRole('button', { name: 'Add item' }));
    expect(screen.getByText('Item 1 has no text')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Save checklist' }).hasAttribute('disabled')).toBe(true);
  });

  it('saves a note, and shows the server message when a change is refused', async () => {
    const onUpdateItem = vi.fn(async () => ({ ok: false, message: 'Note is longer than 1000 characters' }));
    render(
      <ChecklistPanel item={{ callId: 'c', checklist: ITEMS }} onSaveList={vi.fn()} onUpdateItem={onUpdateItem} />,
    );
    fireEvent.click(within(screen.getByTestId('checklist-item-t1')).getByRole('button', { name: 'Add note' }));
    fireEvent.change(screen.getByRole('textbox', { name: 'Note on Hiring' }), { target: { value: 'later' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save note' }));
    await waitFor(() => expect(onUpdateItem).toHaveBeenCalledWith('t1', { Note: 'later' }));
    expect(await screen.findByText('Note is longer than 1000 characters')).toBeTruthy();
  });
});

describe('the Meeting context panel', () => {
  const ITEM = {
    callId: 'c',
    meetingContext: '# Goals',
    meetingContextUpdatedBy: 'alice',
    meetingContextUpdatedAt: '1',
  };

  it('shows the context, and offers no edit when read-only', () => {
    render(<MeetingContextPanel item={ITEM} readOnly onSave={vi.fn()} />);
    expect(screen.getByText('# Goals')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Edit' })).toBeNull();
  });

  it('is hidden when empty and read-only', () => {
    render(<MeetingContextPanel item={{ callId: 'c' }} readOnly onSave={vi.fn()} />);
    expect(screen.queryByTestId('meeting-context-panel')).toBeNull();
  });

  it('saves an edit', async () => {
    const onSave = vi.fn(async () => ({ ok: true }));
    render(<MeetingContextPanel item={ITEM} onSave={onSave} />);
    fireEvent.click(screen.getByRole('button', { name: 'Edit' }));
    fireEvent.change(screen.getByRole('textbox', { name: 'Meeting context, Markdown' }), {
      target: { value: '# Goals\nAgree' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save context' }));
    await waitFor(() => expect(onSave).toHaveBeenCalledWith('# Goals\nAgree'));
  });

  it('does not offer to save a context over the limit', () => {
    render(<MeetingContextPanel item={{ callId: 'c' }} onSave={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: 'Add context' }));
    fireEvent.change(screen.getByRole('textbox', { name: 'Meeting context, Markdown' }), {
      target: { value: 'x'.repeat(20001) },
    });
    expect(screen.getByRole('button', { name: 'Save context' }).hasAttribute('disabled')).toBe(true);
  });
});

describe('the labels and audience', () => {
  it('shows the labels as chips and the audience', () => {
    render(<MeetingLabels item={{ labels: [{ Key: 'project', Value: 'acme' }], audience: 'internal' }} readOnly />);
    expect(screen.getByText('project=acme')).toBeTruthy();
    expect(screen.getByText('Internal')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Edit labels and audience' })).toBeNull();
  });

  it('is hidden when there are none and the viewer cannot add them', () => {
    render(<MeetingLabels item={{}} readOnly onSaveLabels={vi.fn()} onSaveAudience={vi.fn()} />);
    expect(screen.queryByTestId('meeting-labels')).toBeNull();
  });

  it('saves only what changed', async () => {
    const onSaveLabels = vi.fn(async () => ({ ok: true }));
    const onSaveAudience = vi.fn(async () => ({ ok: true }));
    render(
      <MeetingLabels
        item={{ labels: [{ Key: 'project', Value: 'acme' }], audience: 'internal' }}
        onSaveLabels={onSaveLabels}
        onSaveAudience={onSaveAudience}
      />,
    );
    fireEvent.click(screen.getByRole('button', { name: 'Edit labels and audience' }));
    fireEvent.change(screen.getByRole('textbox', { name: /^Labels/ }), {
      target: { value: 'project=acme\nteam=design' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() =>
      expect(onSaveLabels).toHaveBeenCalledWith([
        { Key: 'project', Value: 'acme' },
        { Key: 'team', Value: 'design' },
      ]),
    );
    expect(onSaveAudience).not.toHaveBeenCalled();
  });

  it('does not offer to save a label key the server refuses', () => {
    render(<MeetingLabels item={{}} onSaveLabels={vi.fn()} onSaveAudience={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: 'Edit labels and audience' }));
    fireEvent.change(screen.getByRole('textbox', { name: /^Labels/ }), {
      target: { value: 'Project=x' },
    });
    expect(screen.getByRole('button', { name: 'Save' }).hasAttribute('disabled')).toBe(true);
  });
});
