/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

// The web UI's copy of the validator the configuration store applies to every
// save (lma_config/schema.py in the configuration layer), for a configuration
// editor to run on what it is about to send, so a problem can be shown beside
// the setting before the save rather than coming back as a refusal after it.
//
// The two copies must report the same problems in the same words and order.
// lma-ai-stack/test/fixtures/configuration_schema_cases.json is run through
// both, by pytest and by vitest, so neither can drift unnoticed. The server's
// check is the one that decides; this one only makes it visible sooner.

// How a message names each type. The server's copy uses the same words.
const TYPE_WORDS = {
  object: 'an object',
  array: 'a list',
  string: 'text',
  number: 'a number',
  integer: 'a whole number',
  boolean: 'true or false',
  null: 'null',
};

export const PATH_SEPARATOR = ' › ';

// A path for a message. Not a JSON pointer: keys here routinely hold "/", "#"
// and "." of their own, and a separator that cannot appear in a key keeps the
// message readable.
export const formatPath = (path) => (path.length ? path.join(PATH_SEPARATOR) : '(the value)');

const isObject = (value) => value !== null && typeof value === 'object' && !Array.isArray(value);

const isNumber = (value) => typeof value === 'number' && Number.isFinite(value);

const typeMatches = (expected, value) => {
  switch (expected) {
    case 'object':
      return isObject(value);
    case 'array':
      return Array.isArray(value);
    case 'string':
      return typeof value === 'string';
    case 'boolean':
      return typeof value === 'boolean';
    case 'null':
      return value === null;
    case 'integer':
      return isNumber(value) && Number.isInteger(value);
    case 'number':
      return isNumber(value);
    default:
      return false;
  }
};

const describe = (value) => {
  if (typeof value === 'boolean') return 'a boolean';
  if (value === null || value === undefined) return 'null';
  if (Array.isArray(value)) return 'a list';
  if (isObject(value)) return 'an object';
  if (typeof value === 'string') return 'text';
  if (typeof value === 'number' && !Number.isFinite(value)) return 'a non-finite number';
  return 'a number';
};

// JSON equality: objects key by key and lists item by item.
export const deepEqual = (a, b) => {
  if (a === b) return true;
  if (Array.isArray(a) || Array.isArray(b)) {
    return Array.isArray(a) && Array.isArray(b) && a.length === b.length && a.every((v, i) => deepEqual(v, b[i]));
  }
  if (isObject(a) && isObject(b)) {
    const keys = Object.keys(a);
    return keys.length === Object.keys(b).length && keys.every((key) => key in b && deepEqual(a[key], b[key]));
  }
  return false;
};

const json = (value) => JSON.stringify(value);

const count = (number, singular, plural) => `${number} ${number === 1 ? singular : plural || `${singular}s`}`;

// A text's length in code points, as JSON Schema counts it and as Python's len
// does; String.length would count UTF-16 units.
const lengthOf = (text) => Array.from(text).length;

const search = (pattern, text) => new RegExp(pattern, 'u').test(text);

/**
 * Every problem with `value` against `schema`, as { path, message } in the
 * order the server's copy reports them. `path` is the list of keys (and, for a
 * list item, "item N") down to the value.
 */
export const checkDetailed = (schema, value, path = []) => {
  const problems = [];
  const add = (where, message) => problems.push({ path: where, message });

  const expected = schema.type;
  if (expected !== undefined) {
    const expectedTypes = Array.isArray(expected) ? expected : [expected];
    if (!expectedTypes.some((type) => typeMatches(type, value))) {
      const names = expectedTypes.map((type) => TYPE_WORDS[type] || type).join(' or ');
      add(path, `must be ${names}; got ${describe(value)}`);
      // Nothing below means anything for a value of the wrong type.
      return problems;
    }
  }

  if (Array.isArray(schema.enum) && !schema.enum.some((option) => deepEqual(value, option))) {
    add(path, `must be one of ${schema.enum.map(json).join(', ')}; got ${json(value)}`);
  }

  if (isNumber(value)) {
    if (schema.minimum !== undefined && value < schema.minimum) {
      add(path, `must be at least ${schema.minimum}; got ${value}`);
    }
    if (schema.maximum !== undefined && value > schema.maximum) {
      add(path, `must be at most ${schema.maximum}; got ${value}`);
    }
  }

  if (typeof value === 'string') {
    const length = lengthOf(value);
    if (schema.minLength !== undefined && length < schema.minLength) {
      add(path, schema.minLength > 1 ? `must be at least ${schema.minLength} characters` : 'must not be empty');
    }
    if (schema.maxLength !== undefined && length > schema.maxLength) {
      add(path, `must be at most ${schema.maxLength} characters; got ${length}`);
    }
    if (schema.pattern !== undefined && !search(schema.pattern, value)) {
      add(path, `${json(value)} does not match ${schema.pattern}`);
    }
  }

  if (Array.isArray(value)) {
    if (schema.minItems !== undefined && value.length < schema.minItems) {
      add(path, `must hold at least ${count(schema.minItems, 'item')}`);
    }
    if (schema.maxItems !== undefined && value.length > schema.maxItems) {
      add(path, `must hold at most ${count(schema.maxItems, 'item')}`);
    }
    if (isObject(schema.items)) {
      value.forEach((item, index) => {
        problems.push(...checkDetailed(schema.items, item, [...path, `item ${index + 1}`]));
      });
    }
  }

  if (isObject(value)) {
    (schema.required || []).forEach((name) => {
      if (!(name in value)) add(path, `${json(name)} is required`);
    });
    if (schema.maxProperties !== undefined && Object.keys(value).length > schema.maxProperties) {
      add(path, `must hold at most ${count(schema.maxProperties, 'entry', 'entries')}`);
    }
    const properties = schema.properties || {};
    const patterns = schema.patternProperties || {};
    const additional = schema.additionalProperties === undefined ? true : schema.additionalProperties;
    const namesPattern = isObject(schema.propertyNames) ? schema.propertyNames.pattern : undefined;
    Object.entries(value).forEach(([key, item]) => {
      const child = [...path, key];
      if (namesPattern !== undefined && !search(namesPattern, key)) {
        add(child, `the name does not match ${namesPattern}`);
        return;
      }
      let matched = false;
      if (Object.prototype.hasOwnProperty.call(properties, key)) {
        matched = true;
        problems.push(...checkDetailed(properties[key], item, child));
      }
      Object.entries(patterns).forEach(([pattern, patternSchema]) => {
        if (search(pattern, key)) {
          matched = true;
          problems.push(...checkDetailed(patternSchema, item, child));
        }
      });
      if (matched) return;
      if (additional === false) {
        add(child, 'is not a setting this section has');
      } else if (isObject(additional)) {
        problems.push(...checkDetailed(additional, item, child));
      }
    });
  }

  return problems;
};

/** The problems as the server words them: "<path>: <message>". */
export const check = (schema, value, path = []) =>
  checkDetailed(schema, value, path).map((problem) => `${formatPath(problem.path)}: ${problem.message}`);
