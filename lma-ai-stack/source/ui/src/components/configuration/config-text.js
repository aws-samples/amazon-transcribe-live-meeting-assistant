/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
// The configuration as text, for the page's JSON and YAML views, and the
// setting-by-setting comparison of two or more profiles.
import { CORE_SCHEMA, dump, load } from 'js-yaml';

import { clone, deepEqual, effectiveSection, hasAt, isObject, offered, setAt } from './config-model';

export const TEXT_FORMATS = ['json', 'yaml'];

/**
 * Every section's values, keyed by section name, as JSON or YAML.
 *
 * The text holds each section's whole effective value, as a full-copy profile
 * stores it, in the order the page lists the sections, less the top-level
 * settings this deployment does not offer: the form does not show those either.
 */
export const configurationText = (sections, values, format, settings = {}) => {
  const document = Object.fromEntries(
    sections.map((section) => [section.name, offered(section.schema, values[section.name] || {}, settings)]),
  );
  if (format === 'yaml') return dump(document, { noRefs: true, lineWidth: -1, sortKeys: false });
  return `${JSON.stringify(document, null, 2)}\n`;
};

/**
 * The section values a JSON or YAML text holds, read the way an import is.
 *
 * Returns `{ values, unchanged, withheld }`. `values` has the full effective
 * value of each section the text names: the baseline with the text over it, so
 * a setting removed from the text reads back as its default, as it would after
 * a save. A top-level setting this deployment does not offer is not taken from
 * the text; it keeps its value in `current`, and is listed in `withheld` as a
 * [section, setting] pair if the text named it. `unchanged` lists the sections
 * the text leaves out, which keep their values on the page. Throws an Error
 * whose message says what is wrong with the text; the schema checks are the
 * page's, made on what this returns.
 */
export const readConfigurationText = (text, format, sections, defaults = {}, settings = {}, current = {}) => {
  let parsed;
  try {
    // The YAML 1.2 core schema: booleans, nulls and numbers in their usual
    // spellings, with no dates or other YAML-only types.
    parsed = format === 'yaml' ? load(text, { schema: CORE_SCHEMA }) : JSON.parse(text);
  } catch (err) {
    const where = err.mark ? ` (line ${err.mark.line + 1}, column ${err.mark.column + 1})` : '';
    throw new Error(`It is not valid ${format === 'yaml' ? 'YAML' : 'JSON'}${where}: ${err.reason || err.message}`);
  }
  if (!isObject(parsed)) {
    throw new Error('It must be an object whose keys are section names.');
  }
  const byName = Object.fromEntries(sections.map((section) => [section.name, section]));
  const unknown = Object.keys(parsed).filter((name) => !byName[name]);
  if (unknown.length) {
    throw new Error(`This deployment has no section named ${unknown.join(', ')}, or does not offer it.`);
  }
  const notObjects = Object.keys(parsed).filter((name) => !isObject(parsed[name]));
  if (notObjects.length) {
    throw new Error(`Each section must be an object of its settings: ${notObjects.join(', ')} is not.`);
  }
  const values = {};
  const withheld = [];
  Object.entries(parsed).forEach(([name, typed]) => {
    const { schema } = byName[name];
    const used = offered(schema, typed, settings);
    let effective = effectiveSection(schema, defaults[name], used);
    Object.keys(typed)
      .filter((key) => !Object.prototype.hasOwnProperty.call(used, key))
      .forEach((key) => withheld.push([name, key]));
    // A setting the text cannot hold keeps what the page has for it.
    Object.keys(current[name] || {})
      .filter((key) => !Object.prototype.hasOwnProperty.call(offered(schema, { [key]: true }, settings), key))
      .forEach((key) => {
        if (hasAt(current[name], [key])) effective = setAt(effective, [key], clone(current[name][key]));
      });
    values[name] = effective;
  });
  return {
    values,
    withheld,
    unchanged: sections.map((section) => section.name).filter((name) => !(name in parsed)),
  };
};

// Every value the configurations in `list` do not all hold alike, as
// { path, values }, with `values` in the order of `list`. Objects are compared
// key by key; anything else, a list included, as one value. A key one
// configuration does not have is undefined in it.
const differences = (list, path) => {
  if (list.every(isObject)) {
    const keys = [...new Set(list.flatMap((value) => Object.keys(value)))];
    return keys.flatMap((key) =>
      differences(
        list.map((value) => value[key]),
        [...path, key],
      ),
    );
  }
  return list.every((value) => deepEqual(value, list[0])) ? [] : [{ path, values: list }];
};

/**
 * The settings two or more profiles do not all hold alike, section by section
 * in the page's order. `profiles` lists each profile's section values, keyed by
 * section name. Each difference is { section, path, values }, where `path` is
 * the keys below the section and `values` holds each profile's value there, in
 * the order of `profiles`.
 */
export const compareProfiles = (sections, profiles) =>
  sections.flatMap((section) =>
    differences(
      profiles.map((values) => values[section.name] || {}),
      [],
    ).map((difference) => ({ section: section.name, ...difference })),
  );

// A compared value, for display and export: a string as it is, anything else
// as compact JSON, so that `true` and "true" are told apart; a setting a
// profile does not hold says so.
export const comparedText = (value) => {
  if (value === undefined) return '(not set)';
  return typeof value === 'string' ? value : JSON.stringify(value);
};

// One CSV cell: quoted, with quotes doubled. A text a spreadsheet would read as
// a formula gets a leading apostrophe, so it is shown as the text it is.
const csvCell = (text) => {
  const shown = /^[=+@\t\r]|^-[^0-9.]/.test(text) ? `'${text}` : text;
  return `"${shown.replace(/"/g, '""')}"`;
};

/**
 * A profile comparison as CSV: a header row of Section, Setting and each
 * profile's name, then one row for each difference. `label` names a
 * difference's setting; by default its path.
 */
export const comparisonCsv = (names, comparison, label = (item) => item.path.join(' › ')) =>
  `${[
    ['Section', 'Setting', ...names],
    ...comparison.map((item) => [item.section, label(item), ...item.values.map(comparedText)]),
  ]
    .map((row) => row.map(csvCell).join(','))
    .join('\r\n')}\r\n`;

/**
 * A profile comparison as a JSON document, each value as the profile holds it.
 * A profile that does not hold the setting is left out of its `values`.
 */
export const comparisonDocument = (names, comparison) => ({
  profiles: names,
  differences: comparison.map((item) => ({
    section: item.section,
    path: item.path,
    values: Object.fromEntries(
      names.map((name, index) => [name, item.values[index]]).filter(([, value]) => value !== undefined),
    ),
  })),
});
