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
  const ITEMS = ['Introductions', 'Budget', 'Hiring'];
  const COVERAGE = [
    '- **Introductions**: Covered - everyone said hello',
    '- **Budget**: Partly covered - the total is agreed, the split is not',
    '- **Hiring**: Not covered - no time left',
  ].join('\n');

  it('reads one status per item, in agenda order, from the shipped format', () => {
    expect(agendaCoverageStatuses(COVERAGE, ITEMS)).toEqual([COVERED, PARTLY_COVERED, NOT_COVERED]);
  });

  it('reads the status after an item name that contains a colon or a status word', () => {
    expect(agendaCoverageStatuses('- **What is covered: Q3**: Not covered - skipped', ['What is covered: Q3'])).toEqual(
      [NOT_COVERED],
    );
    expect(agendaCoverageStatuses('Covered calls: Not covered', ['Covered calls'])).toEqual([NOT_COVERED]);
  });

  it('reads lines without bold, lines without the item name, and a status inside the bold', () => {
    expect(
      agendaCoverageStatuses('1. Budget: covered - done\n2. Partially covered - started\n- **Hiring: Covered**', [
        'Budget',
        'Timeline',
        'Hiring',
      ]),
    ).toEqual([COVERED, PARTLY_COVERED, COVERED]);
  });

  it('pairs lines with items by name, ignoring case, numbering and closing punctuation', () => {
    const shuffled = [
      '- **2. budget.**: Partly covered - total agreed',
      '- **Hiring**: Not covered',
      '- **Introductions:** Covered',
    ].join('\n');
    expect(agendaCoverageStatuses(shuffled, ITEMS)).toEqual([COVERED, PARTLY_COVERED, NOT_COVERED]);
  });

  it('pairs lines that give only an item number by that number', () => {
    expect(agendaCoverageStatuses('2. Not covered\n1. Covered - done', ['Budget', 'Hiring'])).toEqual([
      COVERED,
      NOT_COVERED,
    ]);
  });

  it('gives no statuses when the agenda no longer matches the names the summary wrote', () => {
    // The same number of items, edited after the summary was written.
    expect(agendaCoverageStatuses(COVERAGE, ['Introductions', 'Roadmap', 'Hiring'])).toBeNull();
    expect(agendaCoverageStatuses(COVERAGE, ['Welcome', 'Q3 budget', 'Open roles'])).toBeNull();
  });

  it('gives no statuses when two lines claim one item or a number is out of range', () => {
    expect(agendaCoverageStatuses('- **Budget**: Covered\n- **Budget**: Not covered', ['Budget', 'Hiring'])).toBeNull();
    expect(agendaCoverageStatuses('1. Covered\n1. Not covered', ['Budget', 'Hiring'])).toBeNull();
    expect(agendaCoverageStatuses('3. Covered', ['Budget'])).toBeNull();
  });

  it('pairs repeated item names by position', () => {
    const coverage = '- **Q&A**: Covered\n- **Break**: Covered\n- **Q&A**: Not covered';
    expect(agendaCoverageStatuses(coverage, ['Q&A', 'Break', 'Q&A'])).toEqual([COVERED, COVERED, NOT_COVERED]);
  });

  it('gives no statuses when the lines cannot be paired one-to-one with the items', () => {
    expect(agendaCoverageStatuses(COVERAGE, [...ITEMS, 'Wrap-up'])).toBeNull();
    expect(agendaCoverageStatuses('No agenda was set for this meeting.', ['Budget'])).toBeNull();
    expect(agendaCoverageStatuses(null, ITEMS)).toBeNull();
    expect(agendaCoverageStatuses(COVERAGE, [])).toBeNull();
    expect(agendaCoverageStatuses(COVERAGE, undefined)).toBeNull();
  });
});
