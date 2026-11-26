/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import { describe, it, expect } from 'vitest';

import { namesInput, namesProblem, namesSummary, parseShortNames, shortNamesText } from './account-names';

describe('short names', () => {
  it('are typed separated by commas and each kept once, ignoring case and spacing', () => {
    expect(parseShortNames(' Bob ,bob, Bobby  C ,, ')).toEqual(['Bob', 'Bobby C']);
    expect(shortNamesText(['Bob', 'Bobby'])).toBe('Bob, Bobby');
  });
});

describe('namesProblem', () => {
  it('accepts names within the limits', () => {
    expect(namesProblem('Robert Chen', ['Bob', 'Robert Chen'])).toBe('');
    expect(namesProblem('', [])).toBe('');
  });

  it('refuses what the server refuses', () => {
    expect(namesProblem('x'.repeat(101), [])).toMatch(/at most 100/);
    expect(namesProblem('', ['y'.repeat(51)])).toMatch(/at most 50/);
    expect(namesProblem('', ['a', 'b', 'c', 'd', 'e', 'f'])).toMatch(/At most 5/);
    expect(namesProblem('Bob <bob@example.com>', [])).toMatch(/< or >/);
  });
});

describe('namesInput and namesSummary', () => {
  it('sends the names trimmed', () => {
    expect(namesInput('  Robert  Chen ', 'Bob, bob')).toEqual({ displayName: 'Robert Chen', shortNames: ['Bob'] });
  });

  it('shows the display name with the short names, or the account name when there is none', () => {
    expect(namesSummary('Robert Chen', ['Bob'])).toBe('Robert Chen (Bob)');
    expect(namesSummary('', [], 'R. Chen')).toBe('R. Chen');
    expect(namesSummary(null, [])).toBe('-');
  });
});
