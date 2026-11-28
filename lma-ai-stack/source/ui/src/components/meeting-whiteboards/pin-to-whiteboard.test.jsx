/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Adding text to a whiteboard: the change sent (appended to a panel, a new
 * panel, a new whiteboard), who the panel belongs to afterwards, the limits,
 * and a change made again on the newer version when somebody changed the
 * whiteboard meanwhile.
 */
import React from 'react';
import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';

import PinToWhiteboardModal from './PinToWhiteboardModal';
import { NEW_PANEL, markdownPanels, newWhiteboardInput, panelsWithText, titleFrom } from './pin-to-whiteboard';

const board = (overrides = {}) => ({
  WhiteboardId: 'wb1',
  Title: 'Launch',
  Version: 3,
  AssistantUpdates: true,
  CreatedAt: '2026-11-28T10:00:00Z',
  Panels: [
    { PanelId: 'dec', Title: 'Decisions', Kind: 'markdown', Content: '- Ship in March', Source: 'assistant' },
    { PanelId: 'q', Title: 'Open questions', Kind: 'markdown', Content: '', Source: 'user', UpdatedBy: 'pat' },
    { PanelId: 'flow', Title: 'Diagram', Kind: 'mermaid', Content: 'flowchart TD\n a-->b', Source: 'assistant' },
  ],
  ...overrides,
});

describe('the change that adds text', () => {
  it('takes a title from the first line, without Markdown marks', () => {
    expect(titleFrom('## **Pricing** options\nmore')).toBe('Pricing options');
    expect(titleFrom('- first item')).toBe('first item');
    expect(titleFrom('word '.repeat(40)).length).toBeLessThanOrEqual(80);
    expect(titleFrom('word '.repeat(40)).endsWith('…')).toBe(true);
  });

  it('offers only Markdown panels', () => {
    expect(markdownPanels(board()).map((p) => p.PanelId)).toEqual(['dec', 'q']);
  });

  it('appends to a panel, keeping an assistant panel the assistant’s when asked', () => {
    const kept = panelsWithText(board(), { panelId: 'dec', text: '- Price at $10', keepWithAssistant: true });
    expect(kept.panels[0]).toEqual({
      PanelId: 'dec',
      Title: 'Decisions',
      Kind: 'markdown',
      Content: '- Ship in March\n\n- Price at $10',
      Source: 'assistant',
    });
    // The other panels are sent without a Source, so they keep their owners.
    expect(kept.panels[1].Source).toBeUndefined();
    const mine = panelsWithText(board(), { panelId: 'dec', text: '- Price at $10', keepWithAssistant: false });
    expect(mine.panels[0].Source).toBeUndefined();
  });

  it('adds a new panel at the end', () => {
    const { panels } = panelsWithText(board(), { panelId: NEW_PANEL, newTitle: 'Pricing', text: '| a |' });
    expect(panels).toHaveLength(4);
    expect(panels[3]).toEqual({ Title: 'Pricing', Kind: 'markdown', Content: '| a |' });
  });

  it('refuses what the whiteboard rules would', () => {
    expect(panelsWithText(board(), { panelId: NEW_PANEL, newTitle: '', text: 'x' }).problem).toMatch(/title/);
    expect(panelsWithText(board(), { panelId: 'q', text: 'x'.repeat(8001) }).problem).toMatch(/8000/);
    expect(panelsWithText(board(), { panelId: 'q', text: '   ' }).problem).toMatch(/nothing/);
    const full = board({ Panels: Array.from({ length: 12 }, (_, i) => ({ PanelId: `p${i}`, Title: `P${i}` })) });
    expect(panelsWithText(full, { panelId: NEW_PANEL, newTitle: 'x', text: 'x' }).problem).toMatch(/12 panels/);
  });

  it('makes a new whiteboard holding the text', () => {
    expect(newWhiteboardInput({ title: ' Pinned ', panelTitle: 'Answer', text: ' hi ' })).toEqual({
      Title: 'Pinned',
      Template: 'blank',
      Panels: [{ Title: 'Answer', Kind: 'markdown', Content: 'hi' }],
    });
  });
});

const fakeWhiteboards = (items, overrides = {}) => ({
  items,
  maxWhiteboards: 5,
  create: vi.fn(async (input) => ({
    error: null,
    board: { WhiteboardId: 'new', Title: input.Title, Panels: input.Panels },
  })),
  update: vi.fn(async (current, change) => ({ error: null, board: { ...current, Panels: change.Panels } })),
  readVersion: vi.fn(),
  ...overrides,
});

describe('the Add to whiteboard dialog', () => {
  it('adds the text to the panel its title names, on the latest whiteboard', async () => {
    const whiteboards = fakeWhiteboards([board()]);
    const onDone = vi.fn();
    render(
      <PinToWhiteboardModal
        whiteboards={whiteboards}
        text="Is the vendor contract signed?"
        title="Open questions"
        onDismiss={() => {}}
        onDone={onDone}
      />,
    );
    fireEvent.click(screen.getByRole('button', { name: 'Add' }));
    await waitFor(() => expect(onDone).toHaveBeenCalled());
    const [sentBoard, change] = whiteboards.update.mock.calls[0];
    expect(sentBoard.WhiteboardId).toBe('wb1');
    expect(change.Panels[1]).toMatchObject({ PanelId: 'q', Content: 'Is the vendor contract signed?' });
    expect(onDone.mock.calls[0][1]).toBe('Open questions');
  });

  it('makes a new whiteboard when the meeting has none', async () => {
    const whiteboards = fakeWhiteboards([]);
    const onDone = vi.fn();
    render(
      <PinToWhiteboardModal
        whiteboards={whiteboards}
        text="Three options"
        title="Pricing"
        onDismiss={() => {}}
        onDone={onDone}
      />,
    );
    fireEvent.click(screen.getByRole('button', { name: 'Add' }));
    await waitFor(() => expect(onDone).toHaveBeenCalled());
    expect(whiteboards.create).toHaveBeenCalledWith({
      Title: 'Pinned',
      Template: 'blank',
      Panels: [{ Title: 'Pricing', Kind: 'markdown', Content: 'Three options' }],
    });
  });

  it('adds the text to the newer version when somebody changed the whiteboard meanwhile', async () => {
    const newer = board({
      Version: 4,
      Panels: board().Panels.map((p) => (p.PanelId === 'q' ? { ...p, Content: '- Who signs off?' } : p)),
    });
    const whiteboards = fakeWhiteboards([board()], {
      update: vi
        .fn()
        .mockResolvedValueOnce({ error: 'changed', conflict: true })
        .mockImplementation(async (current, change) => ({ error: null, board: { ...current, Panels: change.Panels } })),
      readVersion: vi.fn(async () => newer),
    });
    const onDone = vi.fn();
    render(
      <PinToWhiteboardModal
        whiteboards={whiteboards}
        text="- Budget?"
        title="Open questions"
        onDismiss={() => {}}
        onDone={onDone}
      />,
    );
    fireEvent.click(screen.getByRole('button', { name: 'Add' }));
    await waitFor(() => expect(onDone).toHaveBeenCalled());
    expect(whiteboards.readVersion).toHaveBeenCalledWith('wb1');
    const [second, change] = whiteboards.update.mock.calls[1];
    expect(second.Version).toBe(4);
    expect(change.Panels[1].Content).toBe('- Who signs off?\n\n- Budget?');
  });

  it('says why a change was refused, and keeps the dialog open', async () => {
    const whiteboards = fakeWhiteboards([board()], {
      update: vi.fn(async () => ({ error: 'Too many changes to this meeting in the last minute.', conflict: false })),
    });
    const onDone = vi.fn();
    render(
      <PinToWhiteboardModal
        whiteboards={whiteboards}
        text="x"
        title="Decisions"
        onDismiss={() => {}}
        onDone={onDone}
      />,
    );
    fireEvent.click(screen.getByRole('button', { name: 'Add' }));
    expect(await screen.findByText('Too many changes to this meeting in the last minute.')).toBeTruthy();
    expect(onDone).not.toHaveBeenCalled();
  });
});
