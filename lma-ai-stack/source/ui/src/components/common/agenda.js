/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
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

// An item number at the start of a coverage line or of its bold name: "1.",
// "1)", "Item 1:" -- the summary prompt numbers the agenda it is given.
const ITEM_NUMBER = /^\s*(?:item\s+)?(\d{1,3})(?:[.)]\s+|:\s*)/i;

const itemNumberOf = (text) => {
  const match = ITEM_NUMBER.exec(text);
  return match ? Number(match[1]) : null;
};

// An item name as it is compared: without a leading number, bold, case,
// runs of whitespace or the punctuation that ends it.
const normalizeName = (text) =>
  String(text)
    .replace(/\*\*/g, '')
    .replace(ITEM_NUMBER, '')
    .replace(/\s+/g, ' ')
    .replace(/^[\s.:;,–—-]+|[\s.:;,–—-]+$/g, '')
    .toLowerCase();

// What one coverage line says about its item: the status, and the item's name
// and number where the line gives them; null for a line that gives no status.
// The item's name comes first, and may itself contain a colon or a status
// word, so the status is read after the name: after its bold, when it is bold,
// and otherwise after a colon, or from the start of a line with no name.
const parseCoverageLine = (raw) => {
  const markNumber = itemNumberOf(raw);
  const line = raw.replace(LIST_MARK, '').trim();
  const bold = /^\*\*(.+?)\*\*/.exec(line);
  if (bold) {
    const inner = bold[1];
    const after = statusOf(line.slice(bold[0].length));
    const colon = inner.lastIndexOf(':');
    const status = after ?? (colon >= 0 ? statusOf(inner.slice(colon + 1)) : statusOf(inner));
    if (!status) return null;
    let name = inner;
    if (!after) name = colon >= 0 ? inner.slice(0, colon) : '';
    return { status, name: normalizeName(name) || null, number: markNumber ?? itemNumberOf(name) };
  }
  const plain = line.replace(/\*\*/g, '');
  const parts = plain.split(':');
  // The status after a name and a colon first, then at the very start.
  for (let k = 1; k <= parts.length; k += 1) {
    const i = k % parts.length;
    const status = statusOf(parts.slice(i).join(':'));
    if (status) {
      const name = parts.slice(0, i).join(':');
      return { status, name: normalizeName(name) || null, number: markNumber ?? itemNumberOf(name) };
    }
  }
  return null;
};

// Which agenda item (by index) a coverage line is about, or null when it cannot
// be told. A line that names its item is matched by that name, and a name that
// matches no item -- the agenda was edited or derived again after the summary
// was written -- or several items other than the one at its position settles
// nothing. A line without a name is matched by its item number, and a line with
// neither by its position.
const itemIndexOf = (entry, position, names) => {
  if (entry.name) {
    const matches = names.flatMap((name, i) => (name === entry.name ? [i] : []));
    if (matches.length === 1) return matches[0];
    return matches.includes(position) ? position : null;
  }
  if (entry.number != null) {
    return entry.number >= 1 && entry.number <= names.length ? entry.number - 1 : null;
  }
  return position;
};

/**
 * The status of each agenda item, in agenda order, from the summary's AGENDA
 * COVERAGE section: one line per item, "- **[Agenda item]**: Covered |
 * Partly covered | Not covered - [a few words]", as the shipped prompt asks.
 *
 * `items` are the agenda's items as `parseAgendaItems` returns them. The
 * section is model output, and a person can edit it, so it is read leniently
 * -- list marks, bold, with or without the item's name or number -- and only
 * trusted whole: each line is paired with an item by its name, else its
 * number, else its position, and unless every item gets exactly one line null
 * is returned rather than a guess. That includes an agenda changed after the
 * summary was written, whose item names no longer match the section's.
 */
export const agendaCoverageStatuses = (coverage, items) => {
  if (typeof coverage !== 'string' || !Array.isArray(items) || !items.length) return null;
  const entries = coverage.split(/\r?\n/).map(parseCoverageLine).filter(Boolean);
  if (entries.length !== items.length) return null;
  const names = items.map(normalizeName);
  const statuses = new Array(items.length).fill(null);
  for (let position = 0; position < entries.length; position += 1) {
    const index = itemIndexOf(entries[position], position, names);
    if (index == null || statuses[index]) return null;
    statuses[index] = entries[position].status;
  }
  return statuses;
};
