/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
/**
 * Reading a meeting's agenda.
 *
 * The agenda is stored as plain text, one item per line, because that is what
 * a person types and what an agenda copied from an invite or a document already
 * is. Rendering it as a list is a matter of splitting those lines, which is all
 * this module does.
 *
 * Bullet and numbering marks are stripped for display so that "1. Budget" and
 * "- Budget" both read as the item "Budget" in a numbered list. The stored text
 * is never rewritten; editing starts from it exactly as it was saved.
 */

// "-", "*", "•", "1.", "1)", "a." and "a)" at the start of a line, with the
// whitespace after them.
const LIST_MARK = /^\s*(?:[-*•]|\d{1,3}[.)]|[a-zA-Z][.)])\s+/;

/** The agenda's items, in order, with blank lines and list marks removed. */
export const parseAgendaItems = (text) =>
  (text || '')
    .split(/\r?\n/)
    .map((line) => line.replace(LIST_MARK, '').trim())
    .filter((line) => line.length > 0);

/** Whether a meeting has an agenda at all. An empty string is a cleared one. */
export const hasAgenda = (text) => parseAgendaItems(text).length > 0;

const SOURCE_LABELS = {
  scheduled: 'From scheduling',
  mcp: 'From an MCP client',
  ui: 'Set on this page',
  derived: 'Derived from the transcript',
};

/** A short label for where the stored agenda came from, or null if unknown. */
export const describeAgendaSource = (source) => SOURCE_LABELS[source] || null;

/** The longest agenda the backend stores; enforced again in the resolver. */
export const MAX_AGENDA_CHARS = 10000;

/** How far an agenda item got, as the summary's AGENDA COVERAGE section says. */
export const COVERED = 'covered';
export const PARTLY_COVERED = 'partly';
export const NOT_COVERED = 'not';

// A status word at the start of the text after an item's name.
const STATUS_WORDS = /^(partly covered|partially covered|not covered|covered)\b/i;
const SEPARATORS = /^[\s:–—-]+/;

const statusOf = (text) => {
  const match = STATUS_WORDS.exec(String(text).replace(SEPARATORS, ''));
  if (!match) return null;
  const word = match[1].toLowerCase();
  if (word.startsWith('not')) return NOT_COVERED;
  if (word.startsWith('part')) return PARTLY_COVERED;
  return COVERED;
};

// The status one coverage line gives its item, or null for a line that gives
// none. The item's name comes first, and may itself contain a colon or a
// status word, so the status is read after the name: after its bold, when it
// is bold, and otherwise after a colon.
const lineStatus = (raw) => {
  const line = raw.replace(LIST_MARK, '').trim();
  const bold = /^\*\*(.+?)\*\*/.exec(line);
  if (bold) {
    const inner = bold[1];
    return statusOf(line.slice(bold[0].length)) ?? statusOf(inner.slice(inner.lastIndexOf(':') + 1));
  }
  const plain = line.replace(/\*\*/g, '');
  const parts = plain.split(':');
  for (let i = 0; i < parts.length; i += 1) {
    const status = statusOf(parts.slice(i).join(':'));
    if (status) return status;
  }
  return null;
};

/**
 * The status of each agenda item, in agenda order, from the summary's AGENDA
 * COVERAGE section: one line per item, "- **[Agenda item]**: Covered |
 * Partly covered | Not covered - [a few words]", as the shipped prompt asks.
 *
 * The section is model output, and a person can edit it, so it is read
 * leniently -- list marks, bold, with or without the item's name -- and only
 * trusted whole: unless there is exactly one status line per item there is
 * nothing to pair them by, and null is returned rather than a guess.
 */
export const agendaCoverageStatuses = (coverage, itemCount) => {
  if (typeof coverage !== 'string' || !itemCount) return null;
  const statuses = coverage.split(/\r?\n/).map(lineStatus).filter(Boolean);
  return statuses.length === itemCount ? statuses : null;
};
