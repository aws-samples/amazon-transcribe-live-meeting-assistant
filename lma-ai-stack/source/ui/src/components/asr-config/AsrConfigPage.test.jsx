/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Shape invariants for the ASR Config form.
 *
 * Every switch exists in three places: EMPTY (shape + default), the GraphQL
 * selection set, and the resolver's allow-list. A field present in one but missing
 * from another fails silently rather than loudly — two fields once rendered the
 * literal string "undefined" because they were added to EMPTY and the query but not
 * to the loader. The loader now derives from EMPTY, and these tests cover the rest.
 */
import { describe, it, expect } from 'vitest';

import { EMPTY, getAsrConfigQuery } from './AsrConfigPage';

describe('ASR config form shape', () => {
  it('requests every field it holds state for', () => {
    // A field absent from the selection set is always undefined in the response, so
    // it silently resets to its default on every load and the saved value is lost.
    const missing = Object.keys(EMPTY).filter((field) => !getAsrConfigQuery.includes(field));
    expect(missing).toEqual([]);
  });

  it('is exactly the two switches the resolver accepts', () => {
    // The resolver's ALLOWED_FIELDS is asserted the same way on the Python side, so
    // a field added to one place and not the other fails in both test suites.
    expect(Object.keys(EMPTY).sort()).toEqual(['diarizeVirtualParticipant', 'engineDefaultMicrovm']);
  });

  it('defaults both switches off, so a fresh deployment stays on Amazon Transcribe', () => {
    Object.entries(EMPTY).forEach(([field, value]) => {
      expect(value, `${field} must default to false`).toBe(false);
    });
  });
});
