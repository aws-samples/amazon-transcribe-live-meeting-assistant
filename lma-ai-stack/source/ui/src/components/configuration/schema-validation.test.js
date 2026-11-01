/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The editor's copy of the configuration validator, held to the fixture the
 * server's copy is held to, and to the section schemas the server ships.
 */
import { readFileSync, readdirSync } from 'fs';
import { join } from 'path';
import { describe, expect, it } from 'vitest';

import { check, checkDetailed, deepEqual } from './schema-validation';

// Both files sit outside the UI package, where Vite will not serve them, so
// they are read from disk.
const AI_STACK = join(import.meta.dirname, '../../../../..');
const SCHEMA_DIR = join(AI_STACK, 'source/lambda_layers/configuration_layer/lma_config/schemas');
const fixture = JSON.parse(readFileSync(join(AI_STACK, 'test/fixtures/configuration_schema_cases.json'), 'utf8'));

// The section schemas exactly as the configuration layer ships them.
const SCHEMAS = Object.fromEntries(
  readdirSync(SCHEMA_DIR)
    .filter((name) => name.endsWith('.json'))
    .map((name) => [name.replace('.json', ''), JSON.parse(readFileSync(join(SCHEMA_DIR, name), 'utf8'))]),
);

// Every regular expression a schema holds, wherever it sits.
const patternsOf = (schema) => {
  if (!schema || typeof schema !== 'object') return [];
  const found = [];
  if (typeof schema.pattern === 'string') found.push(schema.pattern);
  Object.keys(schema.patternProperties || {}).forEach((pattern) => found.push(pattern));
  Object.values(schema.properties || {}).forEach((child) => found.push(...patternsOf(child)));
  Object.values(schema.patternProperties || {}).forEach((child) => found.push(...patternsOf(child)));
  ['additionalProperties', 'items', 'propertyNames'].forEach((key) => found.push(...patternsOf(schema[key])));
  return found;
};

describe('schema-validation', () => {
  it('has the shared fixture to check against', () => {
    expect(fixture.cases.length).toBeGreaterThanOrEqual(20);
  });

  it.each(fixture.cases.map((testCase) => [testCase.name, testCase]))(
    'reports what the shared fixture expects: %s',
    (_name, testCase) => {
      expect(check(testCase.schema, testCase.value)).toEqual(testCase.problems);
    },
  );

  it('finds every section schema the layer ships', () => {
    expect(Object.keys(SCHEMAS).sort()).toEqual([
      'asr_engine',
      'call_pricing',
      'chat_buttons',
      'nova_sonic_config',
      'summary_prompt_templates',
    ]);
  });

  it.each(Object.entries(SCHEMAS))('compiles every pattern in %s as the server does', (_path, schema) => {
    // Python's re and JavaScript's RegExp agree on the plain syntax these use;
    // a pattern only one of them accepts would make the editor and the server
    // disagree about what is valid.
    patternsOf(schema).forEach((pattern) => {
      expect(() => new RegExp(pattern, 'u')).not.toThrow();
    });
  });

  it('accepts an empty section for every schema, which is what Reset section to defaults stores', () => {
    Object.values(SCHEMAS).forEach((schema) => {
      expect(check(schema, {})).toEqual([]);
    });
  });

  it('reports each problem with the path of the value it is about', () => {
    const problems = checkDetailed(
      SCHEMAS.call_pricing,
      { regions: { default: { 'transcribe/streaming': { seconds: -1 } } } },
      ['call_pricing'],
    );
    expect(problems).toEqual([
      {
        path: ['call_pricing', 'regions', 'default', 'transcribe/streaming', 'seconds'],
        message: 'must be at least 0; got -1',
      },
    ]);
  });

  it('refuses a number that is not finite, as JSON has no form for one', () => {
    expect(check({ type: 'number' }, Infinity)).toEqual(['(the value): must be a number; got a non-finite number']);
  });

  it('compares as JSON does', () => {
    expect(deepEqual({ a: [1, { b: 2 }] }, { a: [1, { b: 2 }] })).toBe(true);
    expect(deepEqual({ a: 1 }, { a: 1, b: undefined })).toBe(false);
    expect(deepEqual(true, 1)).toBe(false);
  });
});
