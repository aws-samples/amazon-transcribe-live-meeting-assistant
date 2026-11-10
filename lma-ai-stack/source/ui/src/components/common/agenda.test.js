/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import { describe, it, expect } from 'vitest';

import {
  agendaCoverageStatuses,
  COVERED,
  describeAgendaSource,
  hasAgenda,
  NOT_COVERED,
  PARTLY_COVERED,
  parseAgendaItems,
} from './agenda';

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

describe('agendaCoverageStatuses', () => {
  const COVERAGE = [
    '- **Introductions**: Covered - everyone said hello',
    '- **Budget**: Partly covered - the total is agreed, the split is not',
    '- **Hiring**: Not covered - no time left',
  ].join('\n');

  it('reads one status per item, in agenda order, from the shipped format', () => {
    expect(agendaCoverageStatuses(COVERAGE, 3)).toEqual([COVERED, PARTLY_COVERED, NOT_COVERED]);
  });

  it('reads the status after an item name that contains a colon or a status word', () => {
    expect(agendaCoverageStatuses('- **What is covered: Q3**: Not covered - skipped', 1)).toEqual([NOT_COVERED]);
  });

  it('reads lines without bold, lines without the item name, and a status inside the bold', () => {
    expect(
      agendaCoverageStatuses('1. Budget: covered - done\n2. Partially covered - started\n- **Hiring: Covered**', 3),
    ).toEqual([COVERED, PARTLY_COVERED, COVERED]);
  });

  it('gives no statuses when the lines cannot be paired one-to-one with the items', () => {
    expect(agendaCoverageStatuses(COVERAGE, 4)).toBeNull();
    expect(agendaCoverageStatuses('No agenda was set for this meeting.', 1)).toBeNull();
    expect(agendaCoverageStatuses(null, 3)).toBeNull();
    expect(agendaCoverageStatuses(COVERAGE, 0)).toBeNull();
  });
});
