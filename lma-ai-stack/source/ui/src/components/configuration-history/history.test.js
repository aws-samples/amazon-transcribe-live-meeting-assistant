/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import { describe, it, expect } from 'vitest';

import {
  comparisonPair,
  listRevisionsQuery,
  parseChanges,
  pathText,
  sectionsText,
  sourceText,
  valueText,
} from './history';

describe('what a revision changed', () => {
  it('names each section the way the page that edits it does', () => {
    expect(sectionsText(['nova_sonic_config', 'summary_prompt_templates'])).toBe(
      'Nova Sonic, Transcript summary prompts',
    );
  });

  it('shows a section it does not know by its key, rather than hiding it', () => {
    expect(sectionsText(['a_later_section'])).toBe('a_later_section');
  });

  it('tells a revision whose changes are not known from one that changed nothing named', () => {
    expect(sectionsText(null)).toBe('Not known');
    expect(sectionsText([])).toBe('—');
  });
});

describe('how a revision came to be', () => {
  it('names the revision a restore restored', () => {
    expect(sourceText({ Source: 'restore', RestoredFrom: 4 })).toBe('Restored from revision 4');
  });

  it('describes the revision history began with', () => {
    expect(sourceText({ Source: 'backfill' })).toBe('As it stood when history began');
  });
});

describe('a comparison', () => {
  it('reads from the older revision to the newer, whichever was selected first', () => {
    expect(comparisonPair([{ Revision: 9 }, { Revision: 3 }])).toEqual({ FromRevision: 3, ToRevision: 9 });
  });

  it('needs exactly two revisions', () => {
    expect(comparisonPair([{ Revision: 9 }])).toBeNull();
    expect(comparisonPair([])).toBeNull();
  });

  it('shows a changed value so that true and "true" are told apart', () => {
    expect(valueText(true)).toBe('true');
    expect(valueText('true')).toBe('true');
    expect(valueText({ a: 1 })).toBe('{"a":1}');
    expect(valueText(undefined)).toBe('');
  });

  it('shows a path with a separator that a prompt key cannot hold', () => {
    expect(pathText(['summary_prompt_templates', '1#SUMMARY'])).toBe('Transcript summary prompts › 1#SUMMARY');
  });

  it('reads an answer holding no changes as an empty list', () => {
    expect(parseChanges('[]')).toEqual([]);
    expect(parseChanges(null)).toEqual([]);
    expect(parseChanges('not json')).toEqual([]);
  });
});

describe('the history read', () => {
  it('asks for the version a restore is checked against', () => {
    // Without it every restore would be sent against version 0 and refused.
    expect(listRevisionsQuery).toMatch(/listConfigurationRevisions\(Profile: \$Profile\) {\s+Profile\s+Version/);
  });
});
