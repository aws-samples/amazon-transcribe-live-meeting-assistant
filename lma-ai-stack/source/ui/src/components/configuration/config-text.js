/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
// The configuration as text, for the page's JSON and YAML views, and the
// setting-by-setting comparison of two profiles.
import yaml from 'js-yaml';

import { deepEqual, isObject } from './config-model';

export const TEXT_FORMATS = ['json', 'yaml'];

/**
 * Every section's values, keyed by section name, as JSON or YAML.
 *
 * The text holds each section's whole effective value, as a full-copy profile
 * stores it, in the order the page lists the sections.
 */
export const configurationText = (sections, values, format) => {
  const document = Object.fromEntries(sections.map((section) => [section.name, values[section.name] || {}]));
  if (format === 'yaml') return yaml.dump(document, { noRefs: true, lineWidth: -1, sortKeys: false });
  return `${JSON.stringify(document, null, 2)}\n`;
};

/**
 * The section values a JSON or YAML text holds.
 *
 * Returns `{ values, unchanged }`: `values` has one entry for each section the
 * text names, and `unchanged` lists the sections it leaves out, which keep the
 * values they have on the page. Throws an Error whose message says what is
 * wrong with the text; the schema checks are the page's, made on what this
 * returns.
 */
export const readConfigurationText = (text, format, sections) => {
  let parsed;
  try {
    parsed = format === 'yaml' ? yaml.load(text, { schema: yaml.JSON_SCHEMA }) : JSON.parse(text);
  } catch (err) {
    const where = err.mark ? ` (line ${err.mark.line + 1}, column ${err.mark.column + 1})` : '';
    throw new Error(`It is not valid ${format === 'yaml' ? 'YAML' : 'JSON'}${where}: ${err.reason || err.message}`);
  }
  if (!isObject(parsed)) {
    throw new Error('It must be an object whose keys are section names.');
  }
  const known = new Set(sections.map((section) => section.name));
  const unknown = Object.keys(parsed).filter((name) => !known.has(name));
  if (unknown.length) {
    throw new Error(`This deployment has no section named ${unknown.join(', ')}.`);
  }
  const notObjects = Object.keys(parsed).filter((name) => !isObject(parsed[name]));
  if (notObjects.length) {
    throw new Error(`Each section must be an object of its settings: ${notObjects.join(', ')} is not.`);
  }
  return {
    values: parsed,
    unchanged: sections.map((section) => section.name).filter((name) => !(name in parsed)),
  };
};

// Every value that differs between `left` and `right`, as { path, left, right }.
// Objects are compared key by key; anything else, a list included, as one value.
// A key one side does not have is undefined on that side.
const differences = (left, right, path) => {
  if (isObject(left) && isObject(right)) {
    const keys = [...new Set([...Object.keys(left), ...Object.keys(right)])];
    return keys.flatMap((key) => differences(left[key], right[key], [...path, key]));
  }
  return deepEqual(left, right) ? [] : [{ path, left, right }];
};

/**
 * The settings two profiles hold differently, section by section in the page's
 * order. Each is { section, path, left, right }, where `path` is the keys below
 * the section.
 */
export const compareProfiles = (sections, left, right) =>
  sections.flatMap((section) =>
    differences(left[section.name] || {}, right[section.name] || {}, []).map((difference) => ({
      section: section.name,
      ...difference,
    })),
  );
