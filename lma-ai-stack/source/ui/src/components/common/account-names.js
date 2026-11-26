/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
/**
 * The names an LMA account goes by in meetings: a display name and up to five
 * short names, by which a meeting's attendees are matched to its participant
 * list for their consent given in advance.
 *
 * The limits are the server's (lma_consent/account_names.py in the
 * configuration layer), repeated here so a form can say what is wrong before it
 * is sent; the server checks them again. See
 * docs/meeting-consent.md#the-match-is-by-display-name.
 */

export const MAX_DISPLAY_NAME_CHARS = 100;
export const MAX_SHORT_NAMES = 5;
export const MAX_SHORT_NAME_CHARS = 50;

// Control characters, < and >, which the server refuses.
// eslint-disable-next-line no-control-regex
const REFUSED = /[<>\u0000-\u001f\u007f-\u009f]/;

const collapse = (text) => (text || '').split(/\s+/).filter(Boolean).join(' ');
const key = (text) => collapse(text).toLowerCase();

/** Short names typed as one line, separated by commas: trimmed, each kept once ignoring case. */
export const parseShortNames = (text) => {
  const seen = new Set();
  return (text || '')
    .split(',')
    .map(collapse)
    .filter((name) => {
      if (!name || seen.has(key(name))) return false;
      seen.add(key(name));
      return true;
    });
};

/** Short names as the form shows them. */
export const shortNamesText = (names) => (names || []).join(', ');

/** What is wrong with the display name, as the server would refuse it, or ''. */
export const displayNameProblem = (displayName) => {
  if (REFUSED.test(displayName || '')) return 'The display name cannot contain < or >.';
  if (collapse(displayName).length > MAX_DISPLAY_NAME_CHARS) {
    return `The display name must be at most ${MAX_DISPLAY_NAME_CHARS} characters.`;
  }
  return '';
};

/** What is wrong with the short names, as the server would refuse them, or ''. */
export const shortNamesProblem = (displayName, shortNames) => {
  const display = collapse(displayName);
  const shorts = (shortNames || []).filter((name) => key(name) !== key(display));
  if (shorts.some((name) => REFUSED.test(name))) return 'A short name cannot contain < or >.';
  if (shorts.some((name) => name.length > MAX_SHORT_NAME_CHARS)) {
    return `Each short name must be at most ${MAX_SHORT_NAME_CHARS} characters.`;
  }
  if (shorts.length > MAX_SHORT_NAMES) return `At most ${MAX_SHORT_NAMES} short names.`;
  return '';
};

/** What is wrong with the names, as the server would refuse them, or '' when nothing is. */
export const namesProblem = (displayName, shortNames) =>
  displayNameProblem(displayName) || shortNamesProblem(displayName, shortNames);

/** The names as a mutation sends them. */
export const namesInput = (displayName, shortNamesTyped) => ({
  displayName: collapse(displayName),
  shortNames: parseShortNames(shortNamesTyped),
});

/** How a user's names read in a list: "Display name (short, names)", or '-' with none. */
export const namesSummary = (displayName, shortNames, fallback = '') => {
  const display = displayName || fallback || '';
  const shorts = (shortNames || []).length ? ` (${shortNames.join(', ')})` : '';
  return `${display}${shorts}`.trim() || '-';
};
