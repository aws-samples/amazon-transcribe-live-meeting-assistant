/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import { describe, it, expect } from 'vitest';

import {
  compareVersions,
  errorMessage,
  errorType,
  isConflict,
  lineDiff,
  mergeWhiteboard,
  mermaidProblem,
  TEMPLATES,
  panelInputs,
  panelProblem,
  withPanelAdded,
  withPanelEdited,
  withPanelHandedBack,
  withPanelMoved,
  withPanelRemoved,
} from './whiteboard-model';

const panels = [
  { PanelId: 'a', Title: 'Decisions', Kind: 'markdown', Content: 'one', Source: 'assistant', UpdatedBy: 'x' },
  { PanelId: 'b', Title: 'Notes', Kind: 'markdown', Content: '', Source: 'user', UpdatedBy: 'bob' },
];
const board = (overrides = {}) => ({
  WhiteboardId: 'wb1',
  Version: 2,
  CreatedAt: '2026-11-20T10:00:00Z',
  Panels: panels,
  ...overrides,
});

describe('the whiteboard list', () => {
  it('adds a new whiteboard in the order they were made', () => {
    const later = board({ WhiteboardId: 'wb2', CreatedAt: '2026-11-20T11:00:00Z' });
    const earlier = board({ WhiteboardId: 'wb0', CreatedAt: '2026-11-20T09:00:00Z' });
    const list = mergeWhiteboard(mergeWhiteboard([board()], later), earlier);
    expect(list.map((b) => b.WhiteboardId)).toEqual(['wb0', 'wb1', 'wb2']);
  });

  it('keeps the higher version, so a late message never undoes a newer one', () => {
    const list = [board({ Version: 5, Title: 'new' })];
    expect(mergeWhiteboard(list, board({ Version: 4, Title: 'old' }))[0].Title).toBe('new');
    expect(mergeWhiteboard(list, board({ Version: 6, Title: 'newer' }))[0].Title).toBe('newer');
  });

  it('drops a deleted whiteboard and the subscription-only fields', () => {
    expect(mergeWhiteboard([board()], { WhiteboardId: 'wb1', Version: 3, Deleted: true })).toEqual([]);
    const [kept] = mergeWhiteboard([], board({ Owner: 'o', SharedWith: '[s]', Deleted: false }));
    expect(kept).not.toHaveProperty('Owner');
    expect(kept).not.toHaveProperty('SharedWith');
  });
});

describe('the panel list a change sends', () => {
  it('leaves Source out of panels the change does not alter', () => {
    expect(panelInputs(panels).every((p) => !('Source' in p))).toBe(true);
    const edited = withPanelEdited(panels, 'b', { Content: 'mine' });
    expect(edited[1]).toEqual({ PanelId: 'b', Title: 'Notes', Kind: 'markdown', Content: 'mine' });
    expect(edited[0]).not.toHaveProperty('Source');
  });

  it('hands one panel to the assistant and no other', () => {
    const sent = withPanelHandedBack(panels, 'b');
    expect(sent[1].Source).toBe('assistant');
    expect(sent[0]).not.toHaveProperty('Source');
  });

  it('removes, moves and adds panels', () => {
    expect(withPanelRemoved(panels, 'a').map((p) => p.PanelId)).toEqual(['b']);
    expect(withPanelMoved(panels, 'b', -1).map((p) => p.PanelId)).toEqual(['b', 'a']);
    expect(withPanelMoved(panels, 'a', -1).map((p) => p.PanelId)).toEqual(['a', 'b']);
    const added = withPanelAdded(panels, { Title: 'Flow', Kind: 'mermaid' });
    expect(added[2]).toEqual({ Title: 'Flow', Kind: 'mermaid', Content: '' });
  });
});

describe('checks before sending', () => {
  it('accepts diagrams in the subset and refuses the rest, as the server does', () => {
    expect(mermaidProblem('flowchart TD\n  a <-- b')).toBeNull();
    expect(mermaidProblem('')).toBeNull();
    expect(mermaidProblem('pie\n "a": 1')).toMatch(/must start/);
    expect(mermaidProblem('%%{init: {}}%%\nflowchart TD\n a-->b')).toMatch(/directives/);
    expect(mermaidProblem('flowchart TD\n click a call x()')).toMatch(/click/);
    expect(mermaidProblem('flowchart TD\n a["<b>x</b>"]')).toMatch(/HTML/);
    expect(
      mermaidProblem(
        'gantt\n  dateFormat YYYY-MM-DD\n  section Build\n  API (Priya) :api, 2026-12-01, 5d\n  Tests :after api, 2d',
      ),
    ).toBeNull();
    expect(mermaidProblem('gantt\n  Task :t1, 2026-12-01, 1d\n  click t1 href "https://example.com"')).toMatch(/click/);
    expect(mermaidProblem('flowchart TD\n a-->b\n style a fill:url(https://example.com/x.svg)')).toMatch(/fetch/);
  });

  it('offers the templates the server has, in its order', () => {
    expect(TEMPLATES.map((t) => t.name)).toEqual([
      'decisions',
      'process',
      'architecture',
      'comparison',
      'timeline',
      'gantt',
      'user_stories',
      'raci',
      'risks',
      'decision_log',
      'retrospective',
      'blank',
    ]);
  });

  it('checks titles and lengths', () => {
    expect(panelProblem({ Title: ' ', Kind: 'markdown' })).toMatch(/title/);
    expect(panelProblem({ Title: 'x'.repeat(81), Kind: 'markdown' })).toMatch(/80/);
    expect(panelProblem({ Title: 'T', Kind: 'markdown', Content: 'x'.repeat(8001) })).toMatch(/8000/);
    expect(panelProblem({ Title: 'T', Kind: 'mermaid', Content: `graph TD\n${'x'.repeat(6000)}` })).toMatch(/6000/);
    expect(panelProblem({ Title: 'T', Kind: 'markdown', Content: 'fine' })).toBeNull();
  });
});

describe('the history', () => {
  it('diffs lines', () => {
    expect(lineDiff('a\nb\nc', 'a\nc\nd')).toEqual([
      { type: 'same', text: 'a' },
      { type: 'removed', text: 'b' },
      { type: 'same', text: 'c' },
      { type: 'added', text: 'd' },
    ]);
  });

  it('compares each panel of an earlier version with the current whiteboard', () => {
    const earlier = { Panels: [panels[0], { PanelId: 'z', Title: 'Gone', Content: 'x' }] };
    const current = { Panels: [{ ...panels[0], Content: 'two' }, panels[1]] };
    const rows = compareVersions(earlier, current);
    expect(rows.map((r) => [r.panelId, r.status])).toEqual([
      ['a', 'changed since'],
      ['z', 'removed since'],
      ['b', 'added since'],
    ]);
  });
});

describe('refusals', () => {
  it('reads the error type from errorType, or from the message', () => {
    expect(errorType({ errors: [{ errorType: 'WhiteboardConflict', message: 'x' }] })).toBe('WhiteboardConflict');
    expect(errorType({ errors: [{ message: 'WhiteboardLimit: at most 5' }] })).toBe('WhiteboardLimit');
    expect(errorType({ errors: [{ message: 'Unauthorized: you may not' }] })).toBe('Unauthorized');
    expect(isConflict({ errors: [{ errorType: 'WhiteboardConflict' }] })).toBe(true);
  });

  it('says what happened in words, and passes on the message of an invalid change', () => {
    expect(errorMessage({ errors: [{ errorType: 'WhiteboardConflict' }] })).toMatch(/Reload/);
    expect(errorMessage({ errors: [{ errorType: 'WhiteboardInvalid', message: 'A panel needs a title' }] })).toBe(
      'A panel needs a title',
    );
    expect(errorMessage({ errors: [{ message: 'Unauthorized: x' }] })).toMatch(/permission/);
  });
});
