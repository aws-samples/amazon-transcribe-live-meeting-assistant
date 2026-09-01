/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Shape invariants for the ASR Config form.
 *
 * Every knob here exists in four places: EMPTY (shape + default), the GraphQL
 * selection set, the numeric range table, and the resolver's allow-list. A field
 * present in one but missing from another fails silently rather than loudly — two
 * new fields once rendered the literal string "undefined" in their input boxes
 * because they were added to EMPTY and the query but not to the loader. The loader
 * now derives from EMPTY, and these tests cover the remaining pairs.
 */
import { describe, it, expect } from 'vitest';

import { EMPTY, NUMERIC_LIMITS, getAsrConfigQuery } from './AsrConfigPage';

describe('ASR config form shape', () => {
  it('requests every field it holds state for', () => {
    // A field absent from the selection set is always undefined in the response, so
    // it silently resets to its default on every load and the saved value is lost.
    const missing = Object.keys(EMPTY).filter((field) => !getAsrConfigQuery.includes(field));
    expect(missing).toEqual([]);
  });

  it('holds state for every field with a numeric range', () => {
    const missing = Object.keys(NUMERIC_LIMITS).filter((field) => !(field in EMPTY));
    expect(missing).toEqual([]);
  });

  it('defaults every numeric field to blank, meaning "use the calibrated value"', () => {
    // A concrete default here would override the deployed bundle's calibrated
    // operating point for anyone who never touched the form.
    Object.keys(NUMERIC_LIMITS).forEach((field) => {
      expect(EMPTY[field]).toBe('');
    });
  });

  it('never leaves a field undefined, which renders as the string "undefined"', () => {
    Object.entries(EMPTY).forEach(([field, value]) => {
      expect(value, `${field} must have a concrete default`).not.toBe(undefined);
    });
  });
});
