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
const purify = vi.hoisted(() => ({ sanitize: vi.fn((svg) => svg.replace(/<script.*?<\/script>/g, '')) }));

vi.mock('mermaid', () => ({ default: mermaid }));
vi.mock('dompurify', () => ({ default: purify }));

// eslint-disable-next-line import/first
import MermaidDiagram from './MermaidDiagram';

afterEach(() => {
  mermaid.render.mockReset();
});

describe('MermaidDiagram', () => {
  it('draws a diagram in strict mode and sanitizes what Mermaid returns', async () => {
    mermaid.render.mockResolvedValue({ svg: '<svg><g>drawn</g><script>x</script></svg>' });
    render(<MermaidDiagram text={'flowchart TD\n  a-->b'} />);
    const drawn = await screen.findByTestId('whiteboard-diagram');
    expect(drawn.innerHTML).toContain('drawn');
    expect(drawn.innerHTML).not.toContain('script');
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
