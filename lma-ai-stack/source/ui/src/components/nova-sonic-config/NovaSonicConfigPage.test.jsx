/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * What the Nova Sonic Configuration page takes from a read.
 *
 * A read returns the settings and, in the same payload, the profile's version
 * and who saved it last. The version has to reach the save, or a save made
 * against a profile another administrator has since saved replaces their edit;
 * and the provenance has to stay out of the settings, or it is shown as shipped
 * configuration in the read-only default view.
 */
import { describe, it, expect } from 'vitest';

import { settingsOnly, versionOf } from './NovaSonicConfigPage';
import { isConflict } from '../common/config-conflict';

const PROVENANCE = { Version: 7, UpdatedBy: 'admin@example.com', UpdatedAt: '2026-01-01T00:00:00Z' };

describe('the settings in a read', () => {
  it('keeps the settings and drops the provenance', () => {
    expect(settingsOnly({ ...PROVENANCE, voiceId: 'matthew', meetingMode: 'translator' })).toEqual({
      voiceId: 'matthew',
      meetingMode: 'translator',
    });
  });

  it('survives an absent read', () => {
    expect(settingsOnly(null)).toEqual({});
    expect(settingsOnly(undefined)).toEqual({});
  });
});

describe('the version a save is checked against', () => {
  it('is the version the read reported', () => {
    expect(versionOf({ ...PROVENANCE, voiceId: 'matthew' })).toBe(7);
  });

  it('is null when the read reports none, so the save is not checked against a guess', () => {
    expect(versionOf({ voiceId: 'matthew' })).toBeNull();
    expect(versionOf(null)).toBeNull();
    expect(versionOf({ Version: '7' })).toBeNull();
  });
});

describe('a refused save', () => {
  it('is recognised by the prefix the resolver raises with', () => {
    expect(isConflict({ errors: [{ message: 'Conflict: the configuration profile changed' }] })).toBe(true);
  });

  it('is not confused with a failure that merely mentions the word', () => {
    expect(isConflict({ errors: [{ message: 'Failed to update: no Conflict: here' }] })).toBe(false);
    expect(isConflict(new Error('Network error'))).toBe(false);
  });
});
