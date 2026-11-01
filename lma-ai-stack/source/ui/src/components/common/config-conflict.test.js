/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import { describe, expect, it } from 'vitest';

import { isConflict } from './config-conflict';

describe('isConflict', () => {
  it('recognises a refused save by the prefix the resolvers give it', () => {
    expect(isConflict({ errors: [{ message: 'Conflict: the configuration profile was saved by someone' }] })).toBe(
      true,
    );
    expect(isConflict(new Error('Conflict: saved since'))).toBe(true);
  });

  it('does not read an unrelated failure that mentions the word as a conflict', () => {
    expect(isConflict({ errors: [{ message: 'Failed to save: a Conflict: was logged' }] })).toBe(false);
    expect(isConflict(new Error('Network error'))).toBe(false);
    expect(isConflict(undefined)).toBe(false);
  });
});
