/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
/**
 * A meeting's preparation as the page shows and edits it: its labels, audience,
 * meeting context and checklist.
 *
 * The limits are the server's (lma_meeting_preparation/rules.py), repeated here
 * so a form can say what is wrong before it is sent; the server checks every
 * one of them again and its message is what a refused save shows. See
 * docs/meeting-context-and-checklist.md.
 */

export const MAX_LABELS = 20;
export const LABEL_KEY = /^[a-z0-9][a-z0-9._-]{0,62}$/;
export const MAX_LABEL_VALUE_CHARS = 256;
export const MAX_CONTEXT_CHARS = 20000;
export const MAX_CHECKLIST_ITEMS = 100;
export const ITEM_ID = /^[A-Za-z0-9._:-]{1,64}$/;
export const MAX_ITEM_TEXT_CHARS = 500;
export const MAX_NOTE_CHARS = 1000;

export const AUDIENCE_OPTIONS = [
  { value: '', label: 'Unspecified' },
  { value: 'internal', label: 'Internal' },
  { value: 'external', label: 'External' },
];

export const KIND_OPTIONS = [
  { value: 'question', label: 'Question' },
  { value: 'topic', label: 'Topic' },
  { value: 'risk', label: 'Risk' },
  { value: 'confirm', label: 'Confirm' },
  { value: 'decision', label: 'Decision' },
  { value: 'action', label: 'Action' },
];

export const PRIORITY_OPTIONS = [
  { value: 'must', label: 'Must' },
  { value: 'should', label: 'Should' },
  { value: 'could', label: 'Could' },
];

/** Each status, as its chip reads and with the StatusIndicator type it uses. */
export const STATUS_DISPLAY = {
  open: { label: 'Open', type: 'pending' },
  covered: { label: 'Covered', type: 'success' },
  partial: { label: 'Partly covered', type: 'in-progress' },
  deferred: { label: 'Deferred', type: 'stopped' },
  not_applicable: { label: 'Not applicable', type: 'stopped' },
};

export const STATUS_OPTIONS = Object.entries(STATUS_DISPLAY).map(([value, { label }]) => ({ value, label }));

export const statusDisplay = (status) => STATUS_DISPLAY[status] || STATUS_DISPLAY.open;

export const audienceLabel = (audience) =>
  (AUDIENCE_OPTIONS.find((option) => option.value === (audience || '')) || AUDIENCE_OPTIONS[0]).label;

export const optionFor = (options, value) => options.find((option) => option.value === value) || options[0];

/** "Updated by <who>, <when>", or by nobody named. */
export const formatUpdated = (updatedBy, updatedAt) => {
  if (!updatedAt) return null;
  const when = new Date(updatedAt);
  const whenText = Number.isNaN(when.getTime()) ? updatedAt : when.toLocaleString();
  return updatedBy ? `Updated by ${updatedBy}, ${whenText}` : `Updated ${whenText}`;
};

/** Seconds from the meeting's start as m:ss, or h:mm:ss past an hour. */
export const formatElapsed = (seconds) => {
  const total = Math.max(0, Math.floor(Number(seconds) || 0));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = String(total % 60).padStart(2, '0');
  return hours ? `${hours}:${String(minutes).padStart(2, '0')}:${secs}` : `${minutes}:${secs}`;
};

// ── labels ──────────────────────────────────────────────────────────────────

/** Labels as the editor shows them: one key=value per line. */
export const labelsToText = (labels) => (labels || []).map(({ Key, Value }) => `${Key}=${Value}`).join('\n');

/**
 * The labels typed as key=value lines, checked as the server checks them.
 * Returns `{ labels, error }`; `error` is null when they can be saved.
 */
export const parseLabelsText = (text) => {
  const labels = [];
  const seen = new Set();
  const lines = (text || '')
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);
  let error = null;
  lines.some((line) => {
    const at = line.indexOf('=');
    const key = (at >= 0 ? line.slice(0, at) : line).trim();
    const value = at >= 0 ? line.slice(at + 1).trim() : '';
    if (!LABEL_KEY.test(key)) {
      error =
        `"${key}" is not a label key: use lower-case letters, digits, ".", "_" and "-", ` +
        'starting with a letter or digit, up to 63 characters';
    } else if (seen.has(key)) {
      error = `The label key ${key} is given more than once`;
    } else if (value.length > MAX_LABEL_VALUE_CHARS) {
      error = `The value of ${key} is longer than ${MAX_LABEL_VALUE_CHARS} characters`;
    } else {
      seen.add(key);
      labels.push({ Key: key, Value: value });
    }
    return Boolean(error);
  });
  if (!error && labels.length > MAX_LABELS) error = `A meeting has at most ${MAX_LABELS} labels`;
  return { labels, error };
};

/** A meeting list label filter, "key=value" or "key" alone; null when blank. */
export const parseLabelFilter = (text) => {
  const trimmed = (text || '').trim();
  if (!trimmed) return null;
  const at = trimmed.indexOf('=');
  if (at < 0) return { key: trimmed.toLowerCase(), value: null };
  return { key: trimmed.slice(0, at).trim().toLowerCase(), value: trimmed.slice(at + 1).trim() };
};

/**
 * The meetings that match the list's label and audience filters. A label
 * filter matches a meeting with that key (and, given one, that exact value).
 * The audience filter is '' for every meeting, 'unspecified' for meetings with
 * none, or 'internal' / 'external'.
 */
export const filterMeetingsByPreparation = (meetings, { label = '', audience = '' } = {}) => {
  const wanted = parseLabelFilter(label);
  return (meetings || []).filter((meeting) => {
    if (audience === 'unspecified' && meeting.audience) return false;
    if (audience && audience !== 'unspecified' && meeting.audience !== audience) return false;
    if (!wanted) return true;
    return (meeting.labels || []).some(
      ({ Key, Value }) => Key === wanted.key && (wanted.value === null || Value === wanted.value),
    );
  });
};

// ── the checklist ───────────────────────────────────────────────────────────

/** A new ItemId not used by `items`: item-1, item-2, ... */
export const nextItemId = (items) => {
  const used = new Set((items || []).map((item) => item.ItemId));
  let n = (items || []).length + 1;
  while (used.has(`item-${n}`)) n += 1;
  return `item-${n}`;
};

/** A checklist as a replace sends it: each item's defining fields only. */
export const checklistDefinitions = (items) =>
  (items || []).map(({ ItemId, Text, Kind, Priority, Source }) => {
    const definition = { ItemId, Text: (Text || '').trim(), Kind, Priority: Priority || 'should' };
    if (Source !== undefined && Source !== null) definition.Source = Source;
    return definition;
  });

/** What is wrong with a checklist as edited, or null. */
export const checklistProblem = (items) => {
  if ((items || []).length > MAX_CHECKLIST_ITEMS) return `A checklist has at most ${MAX_CHECKLIST_ITEMS} items`;
  const ids = new Set();
  let problem = null;
  (items || []).some((item, index) => {
    const text = (item.Text || '').trim();
    if (!ITEM_ID.test(item.ItemId || '')) problem = `Item ${index + 1} has an id the server does not accept`;
    else if (ids.has(item.ItemId)) problem = `Two items share the id ${item.ItemId}`;
    else if (!text) problem = `Item ${index + 1} has no text`;
    else if (text.length > MAX_ITEM_TEXT_CHARS) {
      problem = `Item ${index + 1} is longer than ${MAX_ITEM_TEXT_CHARS} characters`;
    }
    ids.add(item.ItemId);
    return Boolean(problem);
  });
  return problem;
};

/** `items` with the one at `index` moved by `step` (-1 up, +1 down). */
export const moveItem = (items, index, step) => {
  const target = index + step;
  if (target < 0 || target >= items.length) return items;
  const moved = [...items];
  [moved[index], moved[target]] = [moved[target], moved[index]];
  return moved;
};

/** Counts of each status, for the panel header: "2 covered, 1 open". */
export const statusSummary = (items) => {
  const counts = {};
  (items || []).forEach((item) => {
    const status = STATUS_DISPLAY[item.Status] ? item.Status : 'open';
    counts[status] = (counts[status] || 0) + 1;
  });
  return Object.keys(STATUS_DISPLAY)
    .filter((status) => counts[status])
    .map((status) => `${counts[status]} ${STATUS_DISPLAY[status].label.toLowerCase()}`)
    .join(', ');
};

// ── the Virtual Participant forms ───────────────────────────────────────────

/** The empty preparation fields of the Virtual Participant forms. */
export const EMPTY_VP_PREPARATION = { labelsText: '', audience: '', meetingContext: '', checklist: [] };

/** A Virtual Participant row's preparation, as its forms hold it. */
export const vpPreparationForm = (vp) => ({
  labelsText: labelsToText(vp?.labels),
  audience: vp?.audience || '',
  meetingContext: vp?.meetingContext || '',
  checklist: (vp?.checklist || []).map(({ ItemId, Text, Kind, Priority, Source }) => ({
    ItemId,
    Text,
    Kind,
    Priority: Priority || 'should',
    ...(Source !== undefined && Source !== null ? { Source } : {}),
  })),
});

/** What is wrong with a form's preparation fields, or null. */
export const vpPreparationProblem = (form) => {
  const { error } = parseLabelsText(form?.labelsText);
  if (error) return error;
  if ((form?.meetingContext || '').length > MAX_CONTEXT_CHARS) {
    return `The meeting context is longer than ${MAX_CONTEXT_CHARS} characters`;
  }
  return checklistProblem(form?.checklist);
};

/**
 * The createVirtualParticipant inputs for a form's preparation: only what was
 * given, so a Virtual Participant started without any is created as before.
 */
export const vpPreparationInput = (form) => {
  const input = {};
  const { labels } = parseLabelsText(form?.labelsText);
  if (labels.length) input.labels = labels;
  if (form?.audience) input.audience = form.audience;
  if ((form?.meetingContext || '').trim()) input.meetingContext = form.meetingContext;
  if ((form?.checklist || []).length) input.checklist = checklistDefinitions(form.checklist);
  return input;
};

/**
 * The updateScheduledVirtualParticipant inputs for what an edit changed: each
 * input sent only when it differs from the row, and sent empty to remove it.
 */
export const vpPreparationChanges = (form, original) => {
  const before = vpPreparationForm(original);
  const changes = {};
  const { labels } = parseLabelsText(form?.labelsText);
  if (labelsToText(labels) !== labelsToText(parseLabelsText(before.labelsText).labels)) changes.labels = labels;
  if ((form?.audience || '') !== before.audience) changes.audience = form?.audience || '';
  if ((form?.meetingContext || '') !== before.meetingContext) {
    changes.meetingContext = (form?.meetingContext || '').trim() ? form.meetingContext : '';
  }
  const checklist = checklistDefinitions(form?.checklist);
  if (JSON.stringify(checklist) !== JSON.stringify(checklistDefinitions(before.checklist))) {
    changes.checklist = checklist;
  }
  return changes;
};

/**
 * A meeting as the meeting list exports it: its labels as "key=value" text,
 * and without the meeting context and checklist, which a list row carries only
 * after the meeting was open on the page.
 */
export const exportableMeeting = (meeting) => {
  // eslint-disable-next-line no-unused-vars
  const { labels, meetingContext, meetingContextUpdatedBy, checklist, ...rest } = meeting || {};
  return { ...rest, labels: labelsToText(labels).replace(/\n/g, '; ') };
};
