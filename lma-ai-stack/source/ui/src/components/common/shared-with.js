/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
/**
 * Reading a meeting's or Virtual Participant's SharedWith.
 *
 * The API returns SharedWith as a string, in one of two shapes. A subscription
 * message carries "[a, b]" whenever SharedWith has entries (and an empty or
 * absent value otherwise). A query returns the value as stored: "[a, b]"
 * for a meeting shared through the app, "a,b" (or "a, b") for an older meeting
 * row and for a Virtual Participant row. Both read as the same list of entries,
 * and membership is a comparison of whole entries, as the API's own access
 * checks compare them.
 */

/**
 * The trimmed, non-empty entries of a SharedWith value. "[", "]" and "," only
 * ever separate entries, as in the API: a string is split at each of them, and
 * a list element that contains one is not an entry.
 */
const SEPARATORS = ['[', ']', ','];

export const sharedWithEntries = (value) => {
  if (Array.isArray(value)) {
    return value
      .filter((entry) => typeof entry === 'string' && !SEPARATORS.some((separator) => entry.includes(separator)))
      .map((entry) => entry.trim())
      .filter((entry) => entry);
  }
  if (typeof value !== 'string') return [];
  return value
    .split(/[[\],]/)
    .map((entry) => entry.trim())
    .filter((entry) => entry);
};

/** Whether `username` is one of the entries of a SharedWith value. */
export const isSharedWith = (value, username) => Boolean(username) && sharedWithEntries(value).includes(username);
