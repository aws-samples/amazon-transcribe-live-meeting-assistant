/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import { describe, expect, it } from 'vitest';

import { sourceToDraw } from './MermaidDiagram';
import { mermaidProblem } from './whiteboard-model';

describe('a Gantt chart drawn at the width of its panel', () => {
  it('gets the panel width, and nothing else is changed', () => {
    const chart = 'gantt\n  dateFormat YYYY-MM-DD\n  Task :t1, 2026-12-01, 3d';
    expect(sourceToDraw(chart, 812.4)).toBe(`%%{init: {"gantt": {"useWidth": 812, "useMaxWidth": false, "barHeight": 24, "fontSize": 13, "sectionFontSize": 13}}}%%\n${chart}`);
    expect(sourceToDraw('%% a note\ngantt\n  Task :t1, 2026-12-01, 3d', 100)).toMatch(/"useWidth": 480/);
    expect(sourceToDraw(chart, undefined)).toMatch(/"useWidth": 960/);
    expect(sourceToDraw('flowchart TD\n  a-->b', 800)).toBe('flowchart TD\n  a-->b');
  });

  it('adds the directive only after the source was checked, which may hold none', () => {
    expect(mermaidProblem(sourceToDraw('gantt\n  Task :t1, 2026-12-01, 3d', 800))).toMatch(/directives/);
  });
});
