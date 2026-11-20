/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The Whiteboards tab shows each panel with who it belongs to, sends every
 * change from the version it shows with the whole panel list (Source only on
 * a panel handed to the assistant), and says plainly when the whiteboard has
 * moved on, with a way to reload it.
 */
import React from 'react';
import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';

import WhiteboardsTab from './WhiteboardsTab';

vi.mock('./MermaidDiagram', () => ({
  // eslint-disable-next-line react/prop-types
  default: ({ text }) => <pre data-testid="diagram">{text}</pre>,
}));

const BOARD = {
  CallId: 'call-1',
  WhiteboardId: 'wb1',
  Title: 'Launch plan',
  Template: 'decisions',
  Instructions: '',
  AssistantUpdates: true,
  Version: 4,
  CreatedAt: '2026-11-20T10:00:00Z',
  UpdatedAt: '2026-11-20T10:05:00Z',
  UpdatedBy: 'LMA assistant',
  Panels: [
    {
      PanelId: 'decisions',
      Title: 'Decisions',
      Kind: 'markdown',
      Content: '- Ship in **March**',
      Source: 'assistant',
      UpdatedBy: 'LMA assistant',
      UpdatedAt: '2026-11-20T10:05:00Z',
    },
    {
      PanelId: 'notes',
      Title: 'Notes',
      Kind: 'markdown',
      Content: 'My notes',
      Source: 'user',
      UpdatedBy: 'bob@example.com',
      UpdatedAt: '2026-11-20T10:04:00Z',
    },
    {
      PanelId: 'flow',
      Title: 'Flow',
      Kind: 'mermaid',
      Content: 'flowchart TD\n  a-->b',
      Source: 'assistant',
      UpdatedAt: '2026-11-20T10:05:00Z',
    },
  ],
};

const stateWith = (overrides = {}) => ({
  items: [BOARD],
  whiteboardsEnabled: true,
  assistantAvailable: true,
  maxWhiteboards: 5,
  writable: true,
  loading: false,
  error: null,
  create: vi.fn().mockResolvedValue({ error: null, conflict: false, board: { WhiteboardId: 'wb2' } }),
  update: vi.fn().mockResolvedValue({ error: null, conflict: false }),
  remove: vi.fn().mockResolvedValue({ error: null, conflict: false }),
  reloadBoard: vi.fn().mockResolvedValue(null),
  readVersion: vi.fn(),
  listVersions: vi.fn().mockResolvedValue({ items: [], nextToken: null }),
  setAssistantEnabled: vi.fn().mockResolvedValue(null),
  requestUpdate: vi.fn().mockResolvedValue({ error: null, message: 'Updated 1 whiteboard.' }),
  ...overrides,
});

describe('WhiteboardsTab', () => {
  it('shows each panel with who it belongs to, Markdown rendered and diagrams drawn', () => {
    render(<WhiteboardsTab whiteboards={stateWith()} />);
    expect(screen.getByText('Launch plan')).toBeTruthy();
    expect(screen.getByText('March').tagName).toBe('STRONG');
    expect(screen.getAllByText('Assistant').length).toBe(2);
    expect(screen.getByText('Edited by bob@example.com')).toBeTruthy();
    expect(screen.getByTestId('diagram').textContent).toContain('flowchart TD');
  });

  it('saves an edit from the version shown, with the whole panel list and no Source', async () => {
    const state = stateWith();
    render(<WhiteboardsTab whiteboards={state} />);
    fireEvent.click(screen.getByRole('button', { name: 'Edit Decisions' }));
    const content = screen.getByLabelText('Panel content');
    fireEvent.change(content.querySelector('textarea') || content, { target: { value: '- Ship in April' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(state.update).toHaveBeenCalledTimes(1));
    const [board, change] = state.update.mock.calls[0];
    expect(board.Version).toBe(4);
    expect(change.Panels.map((p) => p.PanelId)).toEqual(['decisions', 'notes', 'flow']);
    expect(change.Panels[0]).toEqual({
      PanelId: 'decisions',
      Title: 'Decisions',
      Kind: 'markdown',
      Content: '- Ship in April',
    });
    expect(change.Panels.some((p) => 'Source' in p)).toBe(false);
  });

  it('hands one panel back to the assistant', async () => {
    const state = stateWith();
    render(<WhiteboardsTab whiteboards={state} />);
    fireEvent.click(screen.getByRole('button', { name: 'More actions for Notes' }));
    fireEvent.click(screen.getByText('Let the assistant update this panel'));
    await waitFor(() => expect(state.update).toHaveBeenCalledTimes(1));
    const panels = state.update.mock.calls[0][1].Panels;
    expect(panels.find((p) => p.PanelId === 'notes').Source).toBe('assistant');
    expect(panels.filter((p) => 'Source' in p).length).toBe(1);
  });

  it('says when the whiteboard moved on, and reloads it', async () => {
    const state = stateWith({
      update: vi.fn().mockResolvedValue({
        error: 'Someone else changed this whiteboard after you opened it. Reload it to see their change.',
        conflict: true,
      }),
    });
    render(<WhiteboardsTab whiteboards={state} />);
    fireEvent.click(screen.getByRole('button', { name: 'Edit Notes' }));
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    expect(await screen.findAllByText(/Someone else changed this whiteboard/)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Reload' }));
    await waitFor(() => expect(state.reloadBoard).toHaveBeenCalledWith('wb1'));
  });

  it('offers no changes to someone who cannot change the meeting', () => {
    render(<WhiteboardsTab whiteboards={stateWith()} readOnly />);
    expect(screen.queryByRole('button', { name: 'Edit Decisions' })).toBeNull();
    expect(screen.queryByText('New whiteboard')).toBeNull();
    expect(screen.queryByText(/Assistant keeps whiteboards updated/)).toBeNull();
  });

  it('turns the assistant on for the meeting and updates on request', async () => {
    const state = stateWith({ whiteboardsEnabled: false });
    render(<WhiteboardsTab whiteboards={state} />);
    expect(screen.queryByRole('button', { name: 'Update now' })).toBeNull();
    fireEvent.click(screen.getByText(/Assistant keeps whiteboards updated/));
    await waitFor(() => expect(state.setAssistantEnabled).toHaveBeenCalledWith(true));

    const on = stateWith();
    render(<WhiteboardsTab whiteboards={on} />);
    fireEvent.click(screen.getByRole('button', { name: 'Update now' }));
    expect(await screen.findByText('Updated 1 whiteboard.')).toBeTruthy();
  });

  it('hides the switch when the deployment turned the assistant off', () => {
    render(<WhiteboardsTab whiteboards={stateWith({ assistantAvailable: false })} />);
    expect(screen.queryByText(/Assistant keeps whiteboards updated/)).toBeNull();
    expect(screen.getByRole('button', { name: 'New whiteboard' })).toBeTruthy();
  });

  it('says the whiteboards went with the transcript, and offers nothing to change', () => {
    render(<WhiteboardsTab whiteboards={stateWith({ items: [], writable: false })} />);
    expect(screen.getByText(/transcript has been deleted/)).toBeTruthy();
    expect(screen.queryByText('New whiteboard')).toBeNull();
  });

  it('makes a whiteboard from a template', async () => {
    const state = stateWith({ items: [] });
    render(<WhiteboardsTab whiteboards={state} />);
    fireEvent.click(screen.getByRole('button', { name: 'New whiteboard' }));
    fireEvent.click(screen.getByRole('button', { name: 'Create' }));
    await waitFor(() => expect(state.create).toHaveBeenCalledTimes(1));
    expect(state.create.mock.calls[0][0]).toEqual({
      Title: '',
      Template: 'decisions',
      Instructions: '',
      AssistantUpdates: true,
    });
  });
});
