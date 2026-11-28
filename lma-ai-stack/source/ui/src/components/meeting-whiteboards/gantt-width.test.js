/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import { describe, expect, it } from 'vitest';

import { sourceToDraw } from './MermaidDiagram';
import { mermaidProblem } from './whiteboard-model';

const directive = (text) => JSON.parse(text.match(/^%%\{init: (.*)\}%%\n/)[1]);

describe('the diagram as it is drawn', () => {
  it('draws a Gantt chart at the width of its panel', () => {
    const chart = 'gantt\n  dateFormat YYYY-MM-DD\n  Task :t1, 2026-12-01, 3d';
    const drawn = sourceToDraw(chart, 812.4);
    expect(drawn.endsWith(`\n${chart}`)).toBe(true);
    expect(directive(drawn).gantt).toMatchObject({ useWidth: 812, useMaxWidth: false });
    expect(directive(sourceToDraw('%% a note\ngantt\n  Task :t1, 2026-12-01, 3d', 100)).gantt.useWidth).toBe(480);
    expect(directive(sourceToDraw(chart, undefined)).gantt.useWidth).toBe(960);
  });

  it('leaves another diagram as it is, but for the dark theme on a dark page', () => {
    expect(sourceToDraw('flowchart TD\n  a-->b', 800)).toBe('flowchart TD\n  a-->b');
    expect(directive(sourceToDraw('flowchart TD\n  a-->b', 800, true))).toEqual({ theme: 'dark' });
  });

  it('adds the directive only after the source was checked, which may hold none', () => {
    expect(mermaidProblem(sourceToDraw('gantt\n  Task :t1, 2026-12-01, 3d', 800))).toMatch(/directives/);
  });
});
