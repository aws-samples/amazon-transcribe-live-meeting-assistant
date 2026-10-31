/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import { describe, it, expect } from 'vitest';

import { describeAgendaSource, hasAgenda, parseAgendaItems } from './agenda';

describe('parseAgendaItems', () => {
  it('splits one item per line and drops blank lines', () => {
    expect(parseAgendaItems('Introductions\n\nBudget\r\nHiring\n')).toEqual(['Introductions', 'Budget', 'Hiring']);
  });

  it('strips bullet and numbering marks so pasted lists read the same', () => {
    expect(parseAgendaItems('1. Budget\n2) Hiring\n- Roadmap\n* Risks\n• Next steps\na. Wrap-up')).toEqual([
      'Budget',
      'Hiring',
      'Roadmap',
      'Risks',
      'Next steps',
      'Wrap-up',
    ]);
  });

  it('keeps words that only look like a mark without the following space', () => {
    expect(parseAgendaItems('A team update\n2026 plan')).toEqual(['A team update', '2026 plan']);
  });

  it('reads a missing or cleared agenda as no items', () => {
    expect(parseAgendaItems(undefined)).toEqual([]);
    expect(parseAgendaItems(null)).toEqual([]);
    expect(parseAgendaItems('')).toEqual([]);
    expect(hasAgenda('  \n ')).toBe(false);
    expect(hasAgenda('Budget')).toBe(true);
  });
});

describe('describeAgendaSource', () => {
  it('labels every source the resolver accepts', () => {
    ['scheduled', 'mcp', 'ui', 'derived'].forEach((source) => {
      expect(describeAgendaSource(source)).toEqual(expect.any(String));
    });
  });

  it('returns null for an unknown source rather than guessing', () => {
    expect(describeAgendaSource('elsewhere')).toBeNull();
    expect(describeAgendaSource(undefined)).toBeNull();
  });
});
