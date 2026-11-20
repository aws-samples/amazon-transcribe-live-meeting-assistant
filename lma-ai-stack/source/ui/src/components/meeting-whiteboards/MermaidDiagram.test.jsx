/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * A diagram is drawn by Mermaid in strict mode and inserted only after
 * DOMPurify; one outside the subset, or one Mermaid cannot draw, is shown as
 * its source.
 */
import React from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';

const mermaid = vi.hoisted(() => ({ initialize: vi.fn(), render: vi.fn() }));
// The sanitizer stands in for DOMPurify: what the page inserts is its answer,
// never what Mermaid returned.
const SANITIZED = '<svg><g>drawn</g></svg>';
const purify = vi.hoisted(() => ({ sanitize: vi.fn() }));

vi.mock('mermaid', () => ({ default: mermaid }));
vi.mock('dompurify', () => ({ default: purify }));

// eslint-disable-next-line import/first
import MermaidDiagram from './MermaidDiagram';

afterEach(() => {
  mermaid.render.mockReset();
});

describe('MermaidDiagram', () => {
  it('draws a diagram in strict mode and sanitizes what Mermaid returns', async () => {
    const raw = '<svg><g>drawn</g><foreignObject>raw</foreignObject></svg>';
    mermaid.render.mockResolvedValue({ svg: raw });
    purify.sanitize.mockReturnValue(SANITIZED);
    render(<MermaidDiagram text={'flowchart TD\n  a-->b'} />);
    const drawn = await screen.findByTestId('whiteboard-diagram');
    expect(purify.sanitize.mock.calls[0][0]).toBe(raw);
    expect(drawn.innerHTML).toBe(SANITIZED);
    expect(mermaid.initialize).toHaveBeenCalledWith({ startOnLoad: false, securityLevel: 'strict' });
    expect(purify.sanitize).toHaveBeenCalled();
  });

  it('shows the source of a diagram Mermaid cannot draw', async () => {
    mermaid.render.mockRejectedValue(new Error('Parse error'));
    render(<MermaidDiagram text={'flowchart TD\n  a-->'} />);
    expect(await screen.findByText(/could not be drawn/)).toBeTruthy();
    expect(screen.getByText(/a-->/)).toBeTruthy();
  });

  it('never draws a diagram outside the subset, and says why', () => {
    render(<MermaidDiagram text={'flowchart TD\n  click a call x()'} />);
    expect(screen.getByText(/click handlers/)).toBeTruthy();
    expect(mermaid.render).not.toHaveBeenCalled();
  });

  it('says when there is no diagram yet', () => {
    render(<MermaidDiagram text="" />);
    expect(screen.getByText('No diagram yet.')).toBeTruthy();
  });
});
