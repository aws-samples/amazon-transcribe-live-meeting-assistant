/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The Whiteboards tab's links to the rest of the page: "Ask the assistant
 * about" a panel (offered to readers too, since it changes nothing), and a
 * whiteboard the page asks to show, selected with its panel picked out.
 */
import React from 'react';
import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';

import WhiteboardsTab from './WhiteboardsTab';

vi.mock('./MermaidDiagram', () => ({
  // eslint-disable-next-line react/prop-types
  default: ({ text }) => <pre data-testid="diagram">{text}</pre>,
}));

const board = (id, title, panels) => ({
  CallId: 'call-1',
  WhiteboardId: id,
  Title: title,
  Template: 'decisions',
  AssistantUpdates: true,
  Version: 1,
  CreatedAt: `2026-11-20T10:0${id.slice(-1)}:00Z`,
  Panels: panels,
});

const FIRST = board('wb1', 'Launch', [
  { PanelId: 'dec', Title: 'Decisions', Kind: 'markdown', Content: '- March', Source: 'assistant' },
]);
const SECOND = board('wb2', 'Pricing', [
  { PanelId: 'cmp', Title: 'Comparison', Kind: 'markdown', Content: '| a |', Source: 'user' },
]);

const whiteboards = (items) => ({
  items,
  writable: true,
  assistantAvailable: true,
  maxWhiteboards: 5,
  whiteboardsEnabled: false,
  loading: false,
  error: null,
});

describe('the Whiteboards tab', () => {
  it('asks the assistant about a panel, for a reader too', () => {
    const onAsk = vi.fn();
    render(<WhiteboardsTab whiteboards={whiteboards([FIRST])} readOnly onAskAboutPanel={onAsk} />);
    fireEvent.click(screen.getByRole('button', { name: 'Ask the assistant about Decisions' }));
    expect(onAsk).toHaveBeenCalledWith(expect.objectContaining({ WhiteboardId: 'wb1' }), FIRST.Panels[0]);
  });

  it('offers no such button without the assistant', () => {
    render(<WhiteboardsTab whiteboards={whiteboards([FIRST])} />);
    expect(screen.queryByRole('button', { name: /Ask the assistant about/ })).toBeNull();
  });

  it('shows the whiteboard the page asks for, its panel picked out', () => {
    const { container, rerender } = render(<WhiteboardsTab whiteboards={whiteboards([FIRST, SECOND])} />);
    expect(screen.getByRole('heading', { name: 'Launch' })).toBeTruthy();
    rerender(
      <WhiteboardsTab
        whiteboards={whiteboards([FIRST, SECOND])}
        selectRequest={{ whiteboardId: 'wb2', panelId: 'cmp', at: 1 }}
      />,
    );
    expect(screen.getByRole('heading', { name: 'Pricing' })).toBeTruthy();
    expect(container.querySelector('.whiteboard-panel-highlighted')?.dataset.panelId).toBe('cmp');
  });
});
