/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import { describe, it, expect } from 'vitest';
import { COLUMN_DEFINITIONS_MAIN, DEFAULT_PREFERENCES, withKnownColumns } from './calls-table-config';

describe('withKnownColumns', () => {
  it('drops a saved column id the meeting list does not have', () => {
    const saved = { pageSize: 30, visibleContent: ['owner', 'callerPhoneNumber', 'summary'], wrapLines: true };
    expect(withKnownColumns(saved)).toEqual({
      ...DEFAULT_PREFERENCES,
      pageSize: 30,
      wrapLines: true,
      visibleContent: ['owner', 'summary'],
    });
  });

  it('keeps every column of the default preferences', () => {
    expect(withKnownColumns(DEFAULT_PREFERENCES)).toEqual(DEFAULT_PREFERENCES);
  });

  it('falls back to the default columns when the saved preferences name none', () => {
    expect(withKnownColumns({ pageSize: 50 }).visibleContent).toEqual(DEFAULT_PREFERENCES.visibleContent);
    expect(withKnownColumns(undefined)).toEqual(DEFAULT_PREFERENCES);
  });
});

describe('COLUMN_DEFINITIONS_MAIN', () => {
  it('has no phone-number column', () => {
    const ids = COLUMN_DEFINITIONS_MAIN.map((column) => column.id);
    expect(ids).not.toContain('callerPhoneNumber');
    expect(COLUMN_DEFINITIONS_MAIN.map((column) => column.header).join(' ')).not.toMatch(/phone/i);
  });
});
