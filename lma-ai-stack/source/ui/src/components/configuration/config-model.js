/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

// The pure logic behind the schema-driven configuration editor, kept apart from
// the components so it can be tested without rendering anything.
//
// The editor works on each section's *effective* values -- what the readers of
// the section will use -- and turns them back into what a profile stores, a
// delta over the shipped baseline, only when it saves. How a section combines
// with the baseline is declared by its schema's `mergeStrategy`:
//
// - "merge": key by key, the configuration store's deep merge. The profile holds
//   only what differs, so a later release that improves a value nobody changed
//   still reaches the deployment.
// - "replace": a non-empty profile section is the whole value, and the shipped
//   one applies only while it is empty. The chat buttons work this way.

import { deepEqual } from './schema-validation';

export { deepEqual };

export const isObject = (value) => value !== null && typeof value === 'object' && !Array.isArray(value);

export const clone = (value) => (value === undefined ? undefined : JSON.parse(JSON.stringify(value)));

// ── paths ──────────────────────────────────────────────────────────────────

export const getAt = (value, path) =>
  path.reduce((node, key) => (node !== null && node !== undefined ? node[key] : undefined), value);

export const hasAt = (value, path) => {
  let node = value;
  return path.every((key) => {
    if (node === null || typeof node !== 'object' || !Object.prototype.hasOwnProperty.call(node, key)) return false;
    node = node[key];
    return true;
  });
};

// A copy of `value` with `newValue` at `path`, creating objects on the way.
export const setAt = (value, path, newValue) => {
  if (path.length === 0) return newValue;
  const [key, ...rest] = path;
  if (Array.isArray(value) && typeof key === 'number') {
    const copy = [...value];
    copy[key] = setAt(value[key], rest, newValue);
    return copy;
  }
  const base = isObject(value) ? value : {};
  return { ...base, [key]: setAt(base[key], rest, newValue) };
};

// A copy of `value` without whatever is at `path`. A path that is not there
// leaves the value as it is.
export const removeAt = (value, path) => {
  if (path.length === 0) return undefined;
  const [key, ...rest] = path;
  if (Array.isArray(value) && typeof key === 'number') {
    if (rest.length === 0) return value.filter((_item, index) => index !== key);
    const copy = [...value];
    copy[key] = removeAt(value[key], rest);
    return copy;
  }
  if (!isObject(value) || !Object.prototype.hasOwnProperty.call(value, key)) return value;
  if (rest.length === 0) {
    const { [key]: _removed, ...others } = value;
    return others;
  }
  return { ...value, [key]: removeAt(value[key], rest) };
};

export const pathKey = (path) => JSON.stringify(path);

// ── a section and the baseline ─────────────────────────────────────────────

// `overlay` over `base`, recursing into objects: the store's deep_merge.
export const deepMerge = (base, overlay) => {
  const merged = { ...(base || {}) };
  Object.entries(overlay || {}).forEach(([key, value]) => {
    merged[key] = isObject(merged[key]) && isObject(value) ? deepMerge(merged[key], value) : clone(value);
  });
  return merged;
};

const isReplace = (schema) => schema?.mergeStrategy === 'replace';

const nonEmpty = (value) => isObject(value) && Object.keys(value).length > 0;

/** What the section's readers use: the baseline with the profile combined over it. */
export const effectiveSection = (schema, defaults, custom) => {
  if (isReplace(schema)) return clone(nonEmpty(custom) ? custom : defaults || {});
  return deepMerge(clone(defaults || {}), custom || {});
};

// The smallest delta that the deep merge turns back into `effective`: every key
// that is not in the baseline, and every value that differs from it.
export const mergeDelta = (base, effective) => {
  if (!isObject(base) || !isObject(effective)) return deepEqual(base, effective) ? undefined : clone(effective);
  const delta = {};
  Object.entries(effective).forEach(([key, value]) => {
    if (!Object.prototype.hasOwnProperty.call(base, key)) {
      delta[key] = clone(value);
      return;
    }
    const inner = mergeDelta(base[key], value);
    if (inner !== undefined) delta[key] = inner;
  });
  return Object.keys(delta).length ? delta : undefined;
};

/**
 * What the profile stores for a section whose effective values are `effective`.
 *
 * Only what differs from the baseline, so nothing the release ships is pinned.
 * A "replace" section stores its whole value, or nothing when it is the
 * shipped one. An empty object is a section that holds nothing of its own,
 * which is what Reset to Defaults stores.
 */
export const storedSection = (schema, defaults, effective) => {
  if (isReplace(schema)) return deepEqual(effective, defaults || {}) ? {} : clone(effective);
  return mergeDelta(defaults || {}, effective || {}) || {};
};

export const differsAt = (defaults, effective, path) => !deepEqual(getAt(defaults, path), getAt(effective, path));

/** The value at `path` as the baseline has it, or, where it has nothing there, gone. */
export const resetAt = (defaults, effective, path) =>
  hasAt(defaults, path) ? setAt(effective, path, clone(getAt(defaults, path))) : removeAt(effective, path);

// How many settings in `effective` differ from `defaults`, counting each value
// that is not an object once, wherever it sits.
export const changedCount = (defaults, effective) => {
  if (isObject(defaults) && isObject(effective)) {
    const keys = new Set([...Object.keys(defaults), ...Object.keys(effective)]);
    return [...keys].reduce((total, key) => total + changedCount(defaults[key], effective[key]), 0);
  }
  return deepEqual(defaults, effective) ? 0 : 1;
};

// ── numbered keys: "N#Label" ───────────────────────────────────────────────

export const NUMBERED_KEY = /^([0-9]+)#(.+)$/;

export const numberedKey = (number, label) => `${number}#${label}`;

/** The "N#Label" entries of an object, in their numbered order. */
export const numberedEntries = (values) =>
  Object.entries(values || {})
    .map(([key, value]) => {
      const match = key.match(NUMBERED_KEY);
      return match ? { key, number: Number(match[1]), label: match[2], value } : null;
    })
    .filter(Boolean)
    .sort((a, b) => a.number - b.number || a.label.localeCompare(b.label));

/** One above the highest number in use, counting entries that are turned off. */
export const nextNumber = (...objects) =>
  objects.reduce(
    (highest, values) => numberedEntries(values).reduce((top, entry) => Math.max(top, entry.number), highest),
    0,
  ) + 1;

/** The entries in this order, numbered 1, 2, 3...: how a "replace" list is reordered. */
export const renumbered = (entries) =>
  Object.fromEntries(entries.map((entry, index) => [numberedKey(index + 1, entry.label), entry.value]));

/**
 * A copy of `effective` with the entry under `key` given the label `label`.
 *
 * In a "replace" section the key simply changes. In a "merge" section a shipped
 * entry cannot be removed, only turned off: the profile is a delta, and leaving
 * the old key out would let the baseline supply it again. So renaming one turns
 * the old key off with the section's `disabledValue` and adds the new key under
 * the same number, which is what the Transcript Summary page has always done.
 */
export const renameEntry = (schema, defaults, effective, key, label) => {
  const match = key.match(NUMBERED_KEY);
  if (!match) return effective;
  const newKey = numberedKey(match[1], label);
  if (newKey === key) return effective;
  const value = effective[key];
  const shipped = Object.prototype.hasOwnProperty.call(defaults || {}, key);
  let next = { ...effective };
  if (!isReplace(schema) && shipped) {
    next[key] = schema.disabledValue;
  } else {
    delete next[key];
  }
  next = { ...next, [newKey]: value };
  return next;
};

/** Whether a numbered entry can be removed outright, rather than only turned off or reset. */
export const canRemoveEntry = (schema, defaults, key) =>
  isReplace(schema) || !Object.prototype.hasOwnProperty.call(defaults || {}, key);

// ── what is shown ──────────────────────────────────────────────────────────

/** Properties in their `order`, then by name. */
export const orderedProperties = (schema) =>
  Object.entries(schema?.properties || {}).sort(
    ([nameA, a], [nameB, b]) => (a.order ?? 1000) - (b.order ?? 1000) || nameA.localeCompare(nameB),
  );

/** Whether a deployment setting a schema names in `availableWhen` has its value. */
export const isAvailable = (schema, settings) => {
  const condition = schema?.availableWhen;
  if (!condition) return true;
  return `${settings?.[condition.setting]}` === `${condition.value}`;
};

/** Whether a field is shown, given the other values of the object it is in. */
export const isShown = (schema, siblings, settings) => {
  if (schema?.hidden) return false;
  if (!isAvailable(schema, settings)) return false;
  const condition = schema?.dependsOn;
  if (!condition) return true;
  return deepEqual(siblings?.[condition.field], condition.value);
};

/** The schema of an object's member: by name, then by pattern, then the rest. */
export const memberSchema = (schema, key) => {
  if (schema?.properties && Object.prototype.hasOwnProperty.call(schema.properties, key)) {
    return schema.properties[key];
  }
  const pattern = Object.keys(schema?.patternProperties || {}).find((candidate) =>
    new RegExp(candidate, 'u').test(key),
  );
  if (pattern) return schema.patternProperties[pattern];
  return isObject(schema?.additionalProperties) ? schema.additionalProperties : {};
};

const TIER_TEXT = {
  immediate: 'A saved change applies the next time it is read, with no delay.',
  short: 'A saved change applies within 30 seconds.',
  standard: 'A saved change applies within about five minutes.',
};

export const appliesText = (tier) => TIER_TEXT[tier] || '';

// ── keeping your changes when someone else saved first ─────────────────────

// Every change from `base` to `mine`: a value set, or a key removed. Objects are
// compared key by key; anything else, a list included, as one value.
export const changesBetween = (base, mine, path = []) => {
  if (isObject(base) && isObject(mine)) {
    const changes = [];
    Object.keys(mine).forEach((key) => changes.push(...changesBetween(base[key], mine[key], [...path, key])));
    Object.keys(base)
      .filter((key) => !Object.prototype.hasOwnProperty.call(mine, key))
      .forEach((key) => changes.push({ path: [...path, key], removed: true }));
    return changes;
  }
  return deepEqual(base, mine) ? [] : [{ path, value: clone(mine) }];
};

/**
 * Your changes, re-applied over what somebody else saved.
 *
 * `base` is what you loaded, `mine` what you have now, `theirs` what is stored
 * now. Each of your changes is applied to `theirs`. Where they changed the same
 * setting to something else, yours is kept and the setting is listed in
 * `conflicts`, so the page can say which of their changes it replaced.
 */
export const rebase = (base, mine, theirs) => {
  let merged = clone(theirs);
  const conflicts = [];
  changesBetween(base, mine).forEach((change) => {
    const theirValue = getAt(theirs, change.path);
    const baseValue = getAt(base, change.path);
    const myValue = change.removed ? undefined : change.value;
    if (!deepEqual(theirValue, baseValue) && !deepEqual(theirValue, myValue)) conflicts.push(change.path);
    merged = change.removed ? removeAt(merged, change.path) : setAt(merged, change.path, change.value);
  });
  return { merged, conflicts };
};
