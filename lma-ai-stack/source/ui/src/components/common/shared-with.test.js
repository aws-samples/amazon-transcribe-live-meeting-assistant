/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import { describe, it, expect } from 'vitest';

import { isSharedWith, sharedWithEntries } from './shared-with';

describe('sharedWithEntries', () => {
  it('reads the "[a, b]" string a meeting carries', () => {
    expect(sharedWithEntries('[a@example.com, b@example.com]')).toEqual(['a@example.com', 'b@example.com']);
  });

  it('reads the comma-separated string a Virtual Participant row carries', () => {
    expect(sharedWithEntries('a@example.com,b@example.com')).toEqual(['a@example.com', 'b@example.com']);
  });

  it('reads a list', () => {
    expect(sharedWithEntries(['a@example.com', ' b@example.com ', ''])).toEqual(['a@example.com', 'b@example.com']);
  });

  it('reads "[", "]" and "," as separators only', () => {
    expect(sharedWithEntries('[x], b@example.com]')).toEqual(['x', 'b@example.com']);
    expect(sharedWithEntries('a@example.com][b@example.com')).toEqual(['a@example.com', 'b@example.com']);
    expect(sharedWithEntries(['a@example.com, b@example.com', '[c@example.com]', 'd@example.com'])).toEqual([
      'd@example.com',
    ]);
  });

  it('reads nothing from an empty or absent value', () => {
    expect(sharedWithEntries('')).toEqual([]);
    expect(sharedWithEntries('[]')).toEqual([]);
    expect(sharedWithEntries(null)).toEqual([]);
    expect(sharedWithEntries(undefined)).toEqual([]);
  });
});

describe('isSharedWith', () => {
  it('matches a whole entry in every shape', () => {
    expect(isSharedWith('[a@example.com, b@example.com]', 'b@example.com')).toBe(true);
    expect(isSharedWith('[b@example.com]', 'b@example.com')).toBe(true);
    expect(isSharedWith('a@example.com,b@example.com', 'a@example.com')).toBe(true);
    expect(isSharedWith(['b@example.com'], 'b@example.com')).toBe(true);
  });

  it('does not match a name that is only part of an entry', () => {
    expect(isSharedWith('[a@example.com, xb@example.com]', 'b@example.com')).toBe(false);
    expect(isSharedWith('a@example.com,b@example.comx', 'b@example.com')).toBe(false);
    expect(isSharedWith('[ab@example.com]', 'b@example.com')).toBe(false);
  });

  it('does not match without a username', () => {
    expect(isSharedWith('[a@example.com]', '')).toBe(false);
    expect(isSharedWith('[a@example.com]', undefined)).toBe(false);
  });
});
