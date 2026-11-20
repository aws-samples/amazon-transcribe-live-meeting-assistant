/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The meeting page's summary profile: the setting, the choice Regenerate
 * offers, and the action items a profile lists. A page given no profiles to
 * choose from behaves as before (CallSummary.test.jsx).
 */
import React from 'react';
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';

import { CallSummary } from './CallPanel';
import ActionItemsTable, { shownActionItems } from './ActionItemsTable';
import { profileLabel } from './SummaryProfileControls';

/* eslint-disable react/prop-types */

const PROFILES = [
  { Name: 'default', Label: 'Default', Description: 'Standard' },
  { Name: 'discovery-session', Label: 'Discovery session', Description: 'Workshops', StructuredActionItems: true },
];

const ITEM = {
  callId: 'c1',
  callSummaryText: JSON.stringify({ SUMMARY: 'We agreed the plan.' }),
  summaryProfile: 'discovery-session',
  actionItems: [
    { Text: 'Send the plan', Owner: 'Priya Shah', DueDate: '2026-12-01', DueText: 'by the first', StartTime: 65 },
    { Text: 'Book a follow-up', Owner: null, DueDate: null, DueText: null, StartTime: null },
  ],
};

/** Choose a value in the one Select of the open dialog. */
const chooseInDialog = (value) => {
  const picker = createWrapper(screen.getByRole('dialog')).findSelect();
  picker.openDropdown();
  picker.selectOptionByValue(value);
};

describe('the action items', () => {
  it('lists each item and says plainly when it has no owner or due date', () => {
    render(<ActionItemsTable items={ITEM.actionItems} />);
    const rows = screen.getAllByRole('row');
    expect(rows).toHaveLength(3);
    expect(within(rows[1]).getByText('Priya Shah')).toBeTruthy();
    expect(within(rows[1]).getByText('2026-12-01 (by the first)')).toBeTruthy();
    expect(within(rows[1]).getByText('1:05')).toBeTruthy();
    expect(within(rows[2]).getByText('No owner')).toBeTruthy();
    expect(within(rows[2]).getByText('No due date')).toBeTruthy();
  });

  it('shows nothing for a summary without action items', () => {
    const { container } = render(<ActionItemsTable items={[]} />);
    expect(container.textContent).toBe('');
    expect(shownActionItems(null)).toEqual([]);
    expect(shownActionItems([{ Text: '  ' }, { Owner: 'x' }])).toEqual([]);
  });
});

describe('the summary profile on the meeting page', () => {
  it('names the profile the meeting is summarised with', () => {
    render(<CallSummary item={ITEM} summaryProfiles={PROFILES} onSummaryProfileChange={vi.fn()} />);
    expect(screen.getByText('Summary profile: Discovery session')).toBeTruthy();
    expect(screen.getByText('Book a follow-up')).toBeTruthy();
    expect(profileLabel(PROFILES, null)).toBe('Default');
    expect(profileLabel(PROFILES, 'retired')).toBe('retired');
  });

  it('changes the meeting profile', async () => {
    const onChange = vi.fn().mockResolvedValue({ ok: true });
    render(<CallSummary item={ITEM} summaryProfiles={PROFILES} onSummaryProfileChange={onChange} />);
    fireEvent.click(screen.getByRole('button', { name: 'Change the summary profile' }));
    chooseInDialog('default');
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(onChange).toHaveBeenCalledWith('default'));
  });

  it('says why a change was refused', async () => {
    const onChange = vi.fn().mockResolvedValue({ ok: false, message: 'There is no summary profile' });
    render(<CallSummary item={ITEM} summaryProfiles={PROFILES} onSummaryProfileChange={onChange} />);
    fireEvent.click(screen.getByRole('button', { name: 'Change the summary profile' }));
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    expect(await screen.findByText('There is no summary profile')).toBeTruthy();
  });

  it('offers no change to a reader who may not change the meeting', () => {
    render(<CallSummary item={ITEM} summaryProfiles={PROFILES} onSummaryProfileChange={vi.fn()} readOnly />);
    expect(screen.queryByRole('button', { name: 'Change the summary profile' })).toBeNull();
  });

  it('regenerates with the profile chosen for that run', async () => {
    const onRegenerate = vi.fn().mockResolvedValue({ Success: true });
    render(<CallSummary item={ITEM} onRegenerate={onRegenerate} summaryProfiles={PROFILES} />);
    fireEvent.click(screen.getByRole('button', { name: 'Regenerate' }));
    const dialog = screen.getByRole('dialog');
    expect(within(dialog).getByText('Regenerate the summary')).toBeTruthy();
    chooseInDialog('default');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Regenerate' }));
    await waitFor(() => expect(onRegenerate).toHaveBeenCalledWith('default'));
  });

  it('starts the regeneration dialog at the meeting profile', async () => {
    const onRegenerate = vi.fn().mockResolvedValue({ Success: true });
    render(<CallSummary item={ITEM} onRegenerate={onRegenerate} summaryProfiles={PROFILES} />);
    fireEvent.click(screen.getByRole('button', { name: 'Regenerate' }));
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Regenerate' }));
    await waitFor(() => expect(onRegenerate).toHaveBeenCalledWith('discovery-session'));
  });

  it('says when the action items could not be listed', () => {
    const state = JSON.stringify({ source: 'generated', status: 'idle', actionItems: 'failed' });
    render(<CallSummary item={{ ...ITEM, actionItems: [], callSummaryState: state }} />);
    expect(screen.getByText(/The action items could not be listed/)).toBeTruthy();
  });

  it('regenerates at once, as before, when there is no profile to choose', async () => {
    const onRegenerate = vi.fn().mockResolvedValue({ Success: true });
    render(<CallSummary item={{ ...ITEM, summaryProfile: null }} onRegenerate={onRegenerate} />);
    fireEvent.click(screen.getByRole('button', { name: 'Regenerate' }));
    await waitFor(() => expect(onRegenerate).toHaveBeenCalledWith());
    expect(screen.queryByText(/Summary profile:/)).toBeNull();
  });
});
