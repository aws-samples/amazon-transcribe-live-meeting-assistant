/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The configuration editor's model. What would be silent if wrong is asserted
 * most: a save that pins shipped values (a later release could never improve
 * them), a removal the baseline quietly undoes, and a conflict resolution that
 * drops somebody's change.
 */
import { describe, expect, it } from 'vitest';

import {
  appliesText,
  canRemoveEntry,
  changedCount,
  changesBetween,
  deepMerge,
  differsAt,
  effectiveSection,
  getAt,
  hasAt,
  isAvailable,
  isShown,
  memberSchema,
  mergeDelta,
  nextNumber,
  numberedEntries,
  orderedProperties,
  rebase,
  rebaseSection,
  removeAt,
  renameEntry,
  renumbered,
  resetAt,
  setAt,
  storedSection,
} from './config-model';

const MERGE = { type: 'object', mergeStrategy: 'merge' };
const REPLACE = { type: 'object', mergeStrategy: 'replace', numberedKeys: true };
const PROMPTS = { type: 'object', mergeStrategy: 'merge', numberedKeys: true, disabledValue: 'NONE' };

describe('paths', () => {
  it('reads, sets and removes values without changing the original', () => {
    const value = { a: { b: 1 }, list: [1, 2] };
    expect(getAt(value, ['a', 'b'])).toBe(1);
    expect(getAt(value, ['a', 'missing', 'deeper'])).toBeUndefined();
    expect(hasAt(value, ['a', 'b'])).toBe(true);
    expect(hasAt(value, ['a', 'c'])).toBe(false);
    expect(setAt(value, ['a', 'c', 'd'], 2)).toEqual({ a: { b: 1, c: { d: 2 } }, list: [1, 2] });
    expect(setAt(value, ['list', 1], 5).list).toEqual([1, 5]);
    expect(removeAt(value, ['a', 'b'])).toEqual({ a: {}, list: [1, 2] });
    expect(removeAt(value, ['list', 0]).list).toEqual([2]);
    expect(removeAt(value, ['nothing', 'here'])).toEqual(value);
    expect(value).toEqual({ a: { b: 1 }, list: [1, 2] });
  });
});

describe('a section and the baseline', () => {
  it('merges a profile over the baseline key by key, as the store does', () => {
    expect(deepMerge({ a: { x: 1, y: 2 }, b: [1] }, { a: { y: 3 }, b: [2] })).toEqual({ a: { x: 1, y: 3 }, b: [2] });
  });

  it('uses a non-empty replace section whole, and the shipped one while it is empty', () => {
    expect(effectiveSection(REPLACE, { '1#A': 'a' }, { '1#B': 'b' })).toEqual({ '1#B': 'b' });
    expect(effectiveSection(REPLACE, { '1#A': 'a' }, {})).toEqual({ '1#A': 'a' });
  });

  it('stores only what differs from the baseline, so nothing shipped is pinned', () => {
    const defaults = { voiceId: 'tiffany', modelId: 'm', nested: { a: 1, b: 2 } };
    const effective = { voiceId: 'matthew', modelId: 'm', nested: { a: 1, b: 3 }, added: true };
    expect(storedSection(MERGE, defaults, effective)).toEqual({ voiceId: 'matthew', nested: { b: 3 }, added: true });
    expect(storedSection(MERGE, defaults, defaults)).toEqual({});
  });

  it('stores a list that differs as the whole list', () => {
    expect(mergeDelta({ list: ['a', 'b'] }, { list: ['a'] })).toEqual({ list: ['a'] });
  });

  it('stores a replace section whole, or nothing when it is the shipped one', () => {
    expect(storedSection(REPLACE, { '1#A': 'a' }, { '1#A': 'a', '2#B': 'b' })).toEqual({ '1#A': 'a', '2#B': 'b' });
    expect(storedSection(REPLACE, { '1#A': 'a' }, { '1#A': 'a' })).toEqual({});
  });

  it('round-trips: what is stored, merged back over the baseline, is what was edited', () => {
    const defaults = { regions: { default: { 'transcribe/streaming': { seconds: 0.1 } }, 'us-west-2': {} } };
    const effective = setAt(defaults, ['regions', 'default', 'transcribe/streaming', 'seconds'], 0.2);
    expect(effectiveSection(MERGE, defaults, storedSection(MERGE, defaults, effective))).toEqual(effective);
  });

  it('marks and resets a single value', () => {
    const defaults = { a: 1, b: { c: 2 } };
    const effective = { a: 1, b: { c: 3 }, added: 4 };
    expect(differsAt(defaults, effective, ['a'])).toBe(false);
    expect(differsAt(defaults, effective, ['b', 'c'])).toBe(true);
    expect(resetAt(defaults, effective, ['b', 'c'])).toEqual({ a: 1, b: { c: 2 }, added: 4 });
    // Something the baseline does not have is reset by removing it.
    expect(resetAt(defaults, effective, ['added'])).toEqual({ a: 1, b: { c: 3 } });
    expect(changedCount(defaults, effective)).toBe(2);
  });
});

describe('when a saved change applies', () => {
  it('comes from the cache tier, unless the schema says it more exactly', () => {
    expect(appliesText('short')).toBe('A saved change applies within 30 seconds.');
    expect(appliesText('immediate', { appliesText: 'At the next meeting.' })).toBe('At the next meeting.');
  });
});

describe('numbered keys', () => {
  it('lists entries in their numbered order', () => {
    const entries = numberedEntries({ '10#Z': 'z', '2#B': 'b', '1#A': 'a', Version: 3 });
    expect(entries.map((entry) => entry.key)).toEqual(['1#A', '2#B', '10#Z']);
    expect(entries[0]).toEqual({ key: '1#A', number: 1, label: 'A', value: 'a' });
  });

  it('numbers a new entry above every number in use, turned-off ones included', () => {
    expect(nextNumber({ '1#A': 'a', '3#C': 'NONE' }, { '2#B': 'b' })).toBe(4);
    expect(nextNumber({})).toBe(1);
  });

  it('renumbers a reordered replace list from one', () => {
    expect(
      renumbered([
        { label: 'B', value: 'b' },
        { label: 'A', value: 'a' },
      ]),
    ).toEqual({ '1#B': 'b', '2#A': 'a' });
  });

  it('renames a shipped summary section by turning the old key off, so the release cannot bring it back', () => {
    const defaults = { '1#SUMMARY': 'shipped' };
    const renamed = renameEntry(PROMPTS, defaults, { '1#SUMMARY': 'mine' }, '1#SUMMARY', 'OVERVIEW');
    expect(renamed).toEqual({ '1#SUMMARY': 'NONE', '1#OVERVIEW': 'mine' });
    expect(storedSection(PROMPTS, defaults, renamed)).toEqual({ '1#SUMMARY': 'NONE', '1#OVERVIEW': 'mine' });
  });

  it('renames an entry back to the shipped title it was renamed from, turning that one back on', () => {
    const defaults = { '1#SUMMARY': 'shipped' };
    expect(
      renameEntry(PROMPTS, defaults, { '1#SUMMARY': 'NONE', '1#OVERVIEW': 'mine' }, '1#OVERVIEW', 'SUMMARY'),
    ).toEqual({
      '1#SUMMARY': 'mine',
    });
  });

  it('renames an added entry, or one in a replace list, by moving it', () => {
    expect(renameEntry(PROMPTS, {}, { '5#MINE': 'p' }, '5#MINE', 'OURS')).toEqual({ '5#OURS': 'p' });
    expect(renameEntry(REPLACE, { '1#A': 'a' }, { '1#A': 'a' }, '1#A', 'B')).toEqual({ '1#B': 'a' });
  });

  it('removes only what the baseline does not ship, except in a replace list', () => {
    expect(canRemoveEntry(PROMPTS, { '1#A': 'a' }, '1#A')).toBe(false);
    expect(canRemoveEntry(PROMPTS, { '1#A': 'a' }, '2#B')).toBe(true);
    expect(canRemoveEntry(REPLACE, { '1#A': 'a' }, '1#A')).toBe(true);
  });
});

describe('what is shown', () => {
  it('orders properties by their order, then name', () => {
    const schema = { properties: { b: { order: 1 }, a: { order: 1 }, c: { order: 0 }, d: {} } };
    expect(orderedProperties(schema).map(([name]) => name)).toEqual(['c', 'a', 'b', 'd']);
  });

  it('shows a field only while the field it depends on has the value', () => {
    const schema = { dependsOn: { field: 'meetingMode', value: 'translator' } };
    expect(isShown(schema, { meetingMode: 'translator' })).toBe(true);
    expect(isShown(schema, { meetingMode: 'normal' })).toBe(false);
    expect(isShown({ hidden: true }, {})).toBe(false);
  });

  it('shows a section only when the deployment setting it names has the value', () => {
    const schema = { availableWhen: { setting: 'AsrEngineAvailable', value: 'true' } };
    expect(isAvailable(schema, { AsrEngineAvailable: 'true' })).toBe(true);
    expect(isAvailable(schema, { AsrEngineAvailable: true })).toBe(true);
    expect(isAvailable(schema, {})).toBe(false);
    expect(isAvailable({}, {})).toBe(true);
  });

  it('finds a member schema by name, then by pattern, then the rest', () => {
    const schema = {
      properties: { known: { type: 'string' } },
      patternProperties: { '^[0-9]+#': { type: 'number' } },
      additionalProperties: { type: 'boolean' },
    };
    expect(memberSchema(schema, 'known').type).toBe('string');
    expect(memberSchema(schema, '1#A').type).toBe('number');
    expect(memberSchema(schema, 'other').type).toBe('boolean');
  });
});

describe('keeping your changes when someone else saved first', () => {
  it('lists every value set and every key removed', () => {
    expect(changesBetween({ a: 1, b: { c: 2 }, d: 4 }, { a: 1, b: { c: 3 }, e: 5 })).toEqual([
      { path: ['b', 'c'], value: 3 },
      { path: ['e'], value: 5 },
      { path: ['d'], removed: true },
    ]);
  });

  it('applies your changes over theirs and keeps theirs everywhere else', () => {
    const base = { voiceId: 'tiffany', promptMode: 'base' };
    const mine = { voiceId: 'matthew', promptMode: 'base' };
    const theirs = { voiceId: 'tiffany', promptMode: 'inject' };
    expect(rebase(base, mine, theirs)).toEqual({
      merged: { voiceId: 'matthew', promptMode: 'inject' },
      conflicts: [],
    });
  });

  it('keeps yours where both changed the same setting, and says so', () => {
    const result = rebase({ voiceId: 'tiffany' }, { voiceId: 'matthew' }, { voiceId: 'amy' });
    expect(result.merged).toEqual({ voiceId: 'matthew' });
    expect(result.conflicts).toEqual([['voiceId']]);
  });

  it('is not a conflict when both made the same change', () => {
    expect(rebase({ a: 1 }, { a: 2 }, { a: 2 }).conflicts).toEqual([]);
  });

  it('keeps a replace list whole when the other save changed it, and names the section', () => {
    // They reordered the buttons; you edited one. Merging by key would list A twice.
    const result = rebaseSection(
      REPLACE,
      { '1#A': 'a', '2#B': 'b' },
      { '1#A': 'mine', '2#B': 'b' },
      { '1#B': 'b', '2#A': 'a' },
    );
    expect(result).toEqual({ merged: { '1#A': 'mine', '2#B': 'b' }, conflicts: [[]] });
    expect(rebaseSection(REPLACE, { '1#A': 'a' }, { '1#A': 'x' }, { '1#A': 'a' }).conflicts).toEqual([]);
  });

  it('re-applies a removal', () => {
    expect(rebase({ '1#A': 'a', '2#B': 'b' }, { '1#A': 'a' }, { '1#A': 'a', '2#B': 'b', '3#C': 'c' }).merged).toEqual({
      '1#A': 'a',
      '3#C': 'c',
    });
  });
});
