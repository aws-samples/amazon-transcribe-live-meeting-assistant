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
import { act, render, screen } from '@testing-library/react';

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
  it('draws a diagram in strict mode with SVG text labels, and sanitizes what Mermaid returns', async () => {
    const raw = '<svg><g>drawn</g><foreignObject>raw</foreignObject></svg>';
    mermaid.render.mockResolvedValue({ svg: raw });
    purify.sanitize.mockReturnValue(SANITIZED);
    render(<MermaidDiagram text={'flowchart TD\n  a-->b'} />);
    const drawn = await screen.findByTestId('whiteboard-diagram');
    expect(purify.sanitize.mock.calls[0][0]).toBe(raw);
    expect(drawn.innerHTML).toBe(SANITIZED);
    // Labels as SVG text: HTML labels sit in <foreignObject>, which the SVG
    // sanitizer removes, leaving a flowchart or mind map with no labels.
    expect(mermaid.initialize).toHaveBeenCalledWith({
      startOnLoad: false,
      securityLevel: 'strict',
      htmlLabels: false,
      flowchart: { htmlLabels: false },
    });
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

  it('draws a Gantt chart once its panel is laid out, at its width, and again when it changes', async () => {
    const observers = [];
    vi.stubGlobal(
      'ResizeObserver',
      class {
        constructor(callback) {
          this.callback = callback;
          observers.push(this);
        }

        observe() {
          this.observing = true;
        }

        disconnect() {
          this.observing = false;
        }
      },
    );
    mermaid.render.mockResolvedValue({ svg: '<svg/>' });
    purify.sanitize.mockReturnValue(SANITIZED);
    render(<MermaidDiagram text={'gantt\n  Task :t1, 2026-12-01, 3d'} />);
    // A tab that is not shown has no width yet: nothing is drawn.
    await new Promise((done) => {
      setTimeout(done, 10);
    });
    expect(mermaid.render).not.toHaveBeenCalled();
    act(() => observers[0].callback([{ contentRect: { width: 815 } }]));
    await screen.findByTestId('whiteboard-diagram');
    expect(mermaid.render.mock.calls[0][1]).toMatch(/"useWidth":800/);
    act(() => observers[0].callback([{ contentRect: { width: 1210 } }]));
    await vi.waitFor(() => expect(mermaid.render.mock.calls.at(-1)[1]).toMatch(/"useWidth":1200/));
    vi.unstubAllGlobals();
  });

  it('says when there is no diagram yet', () => {
    render(<MermaidDiagram text="" />);
    expect(screen.getByText('No diagram yet.')).toBeTruthy();
  });
});
