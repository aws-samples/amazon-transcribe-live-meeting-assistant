/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

// The configuration pages save into one shared profile, and a save made against
// a version of it that another administrator has since replaced is refused by
// the resolver with an error starting "Conflict: ". Matched on that prefix, not
// on the word anywhere in the error, so an unrelated failure whose text happens
// to contain it does not tell an administrator to reload. The two outcomes are a
// message that says what to do and one that says to try again, and getting them
// the wrong way round is worse than either.
export const isConflict = (err) => {
  const messages = (err?.errors ?? []).map((entry) => entry?.message ?? '');
  return [...messages, err?.message ?? ''].some((message) => message.startsWith('Conflict: '));
};

export default isConflict;
