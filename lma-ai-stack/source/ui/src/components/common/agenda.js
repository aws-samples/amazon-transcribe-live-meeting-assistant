/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
/**
 * Reading a meeting's agenda.
 *
 * The agenda is stored as plain text, one item per line, because that is what
 * every source produces without translation: a person typing in the Agenda
 * panel, a calendar invite pasted into the scheduling form or passed by an MCP
 * client, and the model that reads one out of the transcript. Rendering it as a
 * list is a matter of splitting those lines, which is all this module does.
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
