/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The display rules of the meeting cost estimate.
 *
 * Meeting costs are mostly fractions of a cent, so the formatting is where a
 * reader is most easily misled: a priced line shown as $0.0000 reads as free,
 * and an unpriced one shown as a number reads as known.
 */
import { describe, expect, it } from 'vitest';

import {
  contextLabel,
  estimateCaveats,
  formatCost,
  formatQuantity,
  formatUnitPrice,
  groupLines,
  serviceLabel,
  tableRows,
} from './call-cost';

const ESTIMATE = {
  Currency: 'USD',
  TotalCost: 0.1234,
  ContextTotals: [
    { Context: 'Summary', Cost: 0.02 },
    { Context: 'Transcription', Cost: 0.1 },
  ],
  Items: [
    { Context: 'Summary', Service: 'bedrock/m', Unit: 'inputTokens', Quantity: 1000, Priced: true, Chargeable: true },
    { Context: 'Summary', Service: 'bedrock/x', Unit: 'inputTokens', Quantity: 10, Priced: false, Chargeable: true },
    {
      Context: 'Transcription',
      Service: 'transcribe/streaming',
      Unit: 'seconds',
      Quantity: 600,
      Priced: true,
      Chargeable: true,
    },
  ],
  UnpricedCount: 1,
  RegionsWithoutListedPrices: ['eu-west-1'],
  BaseRegion: 'us-east-1',
};

describe('formatCost', () => {
  it('shows four decimal places', () => {
    expect(formatCost(0.12345)).toBe('$0.1235');
    expect(formatCost(0)).toBe('$0.0000');
  });

  it('never rounds a priced line down to zero', () => {
    expect(formatCost(0.00003)).toBe('< $0.0001');
  });

  it('says N/A for a line with no cost', () => {
    expect(formatCost(null)).toBe('N/A');
    expect(formatCost(undefined)).toBe('N/A');
  });
});

describe('formatUnitPrice', () => {
  it('keeps the precision a per-token price needs', () => {
    expect(formatUnitPrice(0.000001)).toBe('$0.000001');
    expect(formatUnitPrice(0.0001667)).toBe('$0.0001667');
  });

  it('never writes a price in exponent form, however small', () => {
    expect(formatUnitPrice(7.5e-8)).toBe('$0.000000075');
    expect(formatUnitPrice(1e-8)).toBe('$0.00000001');
    expect(formatUnitPrice(2.76944e-5)).toBe('$0.00002769');
  });

  it('keeps the zeros of a whole-number price', () => {
    expect(formatUnitPrice(1000)).toBe('$1,000');
    expect(formatUnitPrice(0)).toBe('$0');
  });

  it('says so when there is no price', () => {
    expect(formatUnitPrice(null)).toBe('No price');
  });
});

describe('grouping', () => {
  it('orders contexts as the panel lists them, each with its subtotal', () => {
    const groups = groupLines(ESTIMATE);
    expect(groups.map((g) => g.context)).toEqual(['Transcription', 'Summary']);
    expect(groups[1].subtotal).toBe(0.02);
    expect(groups[1].unpriced).toBe(1);
  });

  it('puts a subtotal row after each group', () => {
    const rows = tableRows(ESTIMATE);
    expect(rows.map((r) => r.kind)).toEqual(['line', 'subtotal', 'line', 'line', 'subtotal']);
    expect(new Set(rows.map((r) => r.id)).size).toBe(rows.length);
  });

  it('copes with an estimate that has no lines', () => {
    expect(tableRows({ Items: [] })).toEqual([]);
    expect(tableRows(null)).toEqual([]);
  });
});

describe('labels', () => {
  it('names contexts and services for a reader', () => {
    expect(contextLabel('VirtualParticipant')).toBe('Virtual Participant');
    expect(contextLabel('Unheard')).toBe('Unheard');
    expect(serviceLabel('bedrock/global.anthropic.claude-haiku-4-5-20251001-v1:0')).toBe(
      'Amazon Bedrock — global.anthropic.claude-haiku-4-5-20251001-v1:0',
    );
    expect(serviceLabel('something/else')).toBe('something/else');
  });

  it('formats quantities with separators', () => {
    expect(formatQuantity(1234567.891)).toBe('1,234,567.89');
  });
});

describe('estimateCaveats', () => {
  it('states the unpriced lines and the regions priced at another region', () => {
    expect(estimateCaveats(ESTIMATE)).toEqual([
      '1 line has no price in the price table and is not included in the total.',
      'The price table lists no prices for eu-west-1; us-east-1 prices were used for its usage.',
    ]);
  });

  it('is empty for a complete estimate', () => {
    expect(estimateCaveats({ UnpricedCount: 0, RegionsWithoutListedPrices: [] })).toEqual([]);
  });
});
