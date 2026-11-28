/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * A meeting's whiteboards, without React: the list as the page holds it, the
 * panel lists a change sends, the checks the page makes before sending, a line
 * diff for the history, and the server's refusals in words. The server applies
 * the same rules (lma_whiteboards/rules.py); these only save a round trip.
 * See docs/meeting-whiteboards.md.
 */

export const LIMITS = {
  panels: 12,
  title: 120,
  panelTitle: 80,
  instructions: 1000,
  markdown: 8000,
  mermaid: 6000,
};

export const KIND_MARKDOWN = 'markdown';
export const KIND_MERMAID = 'mermaid';
export const SOURCE_USER = 'user';
export const SOURCE_ASSISTANT = 'assistant';

/** The shipped templates, as rules.TEMPLATES names them. */
export const TEMPLATES = [
  {
    name: 'decisions',
    label: 'Decisions and open questions',
    description: 'What was decided, what is still open, and notes worth keeping.',
  },
  {
    name: 'process',
    label: 'Process diagram',
    description: 'A flowchart of the process or system being discussed, with notes.',
  },
  {
    name: 'architecture',
    label: 'Architecture diagram',
    description:
      'The components of the system being discussed and how they connect, ' +
      "with each component's responsibility and the design decisions.",
  },
  { name: 'comparison', label: 'Comparison', description: 'The options being compared, with notes.' },
  { name: 'timeline', label: 'Timeline', description: 'Dates and milestones mentioned, as a timeline, with notes.' },
  {
    name: 'gantt',
    label: 'Project plan (Gantt chart)',
    description: 'The work agreed, with owners, dates and dependencies, as a Gantt chart.',
  },
  {
    name: 'user_stories',
    label: 'User stories',
    description: 'The user stories discussed, each with its acceptance criteria.',
  },
  {
    name: 'raci',
    label: 'RACI matrix',
    description: 'Who is Responsible, Accountable, Consulted and Informed for each piece of work.',
  },
  {
    name: 'risks',
    label: 'Risk register',
    description: 'The risks raised, with their likelihood, impact, owner and mitigation.',
  },
  {
    name: 'decision_log',
    label: 'Decision log',
    description:
      'Each decision as a short decision record: its context, the decision, ' +
      'its consequences and the alternatives considered.',
  },
  {
    name: 'retrospective',
    label: 'Retrospective',
    description: 'What went well, what to improve, and the actions agreed.',
  },
  { name: 'blank', label: 'Blank', description: 'One Markdown panel to start from.' },
];
export const DEFAULT_TEMPLATE = 'decisions';

export const MERMAID_TYPES = ['flowchart', 'graph', 'sequenceDiagram', 'timeline', 'mindmap', 'gantt'];
/** The statements that carry CSS in the subset, by their first word, lowered (rules.STYLE_STATEMENTS). */
const STYLE_STATEMENTS = ['style', 'classdef', 'linkstyle'];
/** A label within a statement: quoted text, or a node's text in brackets (rules._LABEL). */
const LABEL = /"[^"]*"|\[[^\]]*\]|\([^)]*\)|\{[^}]*\}/g;

const byCreation = (a, b) =>
  String(a.CreatedAt || '').localeCompare(String(b.CreatedAt || '')) ||
  String(a.WhiteboardId).localeCompare(String(b.WhiteboardId));

/**
 * The list after one whiteboard arrives (a subscription message or a mutation's
 * answer): the higher version wins, so a late message never undoes a newer
 * one; a deleted whiteboard leaves the list.
 */
export const mergeWhiteboard = (list, incoming) => {
  if (!incoming?.WhiteboardId) return list;
  const others = (list || []).filter((b) => b.WhiteboardId !== incoming.WhiteboardId);
  if (incoming.Deleted) return others;
  const held = (list || []).find((b) => b.WhiteboardId === incoming.WhiteboardId);
  if (held && Number(held.Version) > Number(incoming.Version)) return list;
  const { Owner, SharedWith, Deleted, ...board } = incoming;
  return [...others, board].sort(byCreation);
};

/**
 * A panel as a change sends it. Source is left out unless given, so a panel the
 * change does not alter keeps its owner; an altered one becomes the sender's.
 */
const asInput = (panel, source) => ({
  PanelId: panel.PanelId,
  Title: panel.Title,
  Kind: panel.Kind || KIND_MARKDOWN,
  Content: panel.Content || '',
  ...(source ? { Source: source } : {}),
});

export const panelInputs = (panels) => (panels || []).map((panel) => asInput(panel));

/** The whole panel list with one panel's title, kind or content changed. */
export const withPanelEdited = (panels, panelId, change) =>
  (panels || []).map((panel) => asInput(panel.PanelId === panelId ? { ...panel, ...change } : panel));

/** The whole panel list with one panel handed to the assistant. */
export const withPanelHandedBack = (panels, panelId) =>
  (panels || []).map((panel) => asInput(panel, panel.PanelId === panelId ? SOURCE_ASSISTANT : undefined));

export const withPanelRemoved = (panels, panelId) =>
  (panels || []).filter((panel) => panel.PanelId !== panelId).map((panel) => asInput(panel));

/** The whole panel list with one panel moved by `delta` places (-1 up, 1 down). */
export const withPanelMoved = (panels, panelId, delta) => {
  const list = [...(panels || [])];
  const index = list.findIndex((panel) => panel.PanelId === panelId);
  const target = index + delta;
  if (index < 0 || target < 0 || target >= list.length) return panelInputs(list);
  [list[index], list[target]] = [list[target], list[index]];
  return panelInputs(list);
};

/** The whole panel list with a new panel at the end; it has no PanelId until stored. */
export const withPanelAdded = (panels, { Title, Kind = KIND_MARKDOWN, Content = '' }) => [
  ...panelInputs(panels),
  { Title, Kind, Content },
];

/** Why a diagram is outside the subset a panel may hold, or null. Mirrors rules.check_mermaid. */
export const mermaidProblem = (text) => {
  const lines = String(text || '')
    .split('\n')
    .map((line) => line.trim());
  const meaningful = lines.filter((line) => line && !line.startsWith('%%'));
  if (meaningful.length === 0) return null;
  const first = meaningful[0];
  if (!MERMAID_TYPES.some((kind) => first === kind || first.startsWith(`${kind} `))) {
    return `A diagram must start with one of: ${MERMAID_TYPES.join(', ')}.`;
  }
  if (String(text).includes('%%{') || lines.includes('---')) {
    return 'A diagram may not hold Mermaid directives or front matter.';
  }
  // A line may hold several statements separated by ";".
  // A ";" in a label (quoted, or in a node's brackets) does not separate statements.
  // Only a flowchart's brackets and quotes are node labels (rules._statements).
  const labelled = ['flowchart', 'graph'].includes(first.split(/\s+/)[0]);
  const statements = meaningful.flatMap((line) => {
    const masked = labelled ? line.replace(LABEL, (label) => ' '.repeat(label.length)) : line;
    const parts = [];
    let start = 0;
    [...masked].forEach((character, index) => {
      if (character === ';') {
        parts.push(line.slice(start, index));
        start = index + 1;
      }
    });
    parts.push(line.slice(start));
    return parts.map((part) => part.trim()).filter(Boolean);
  });
  if (
    statements.some((statement) =>
      ['click', 'callback', 'link', 'links', 'href'].includes(statement.split(/\s+/)[0].toLowerCase()),
    )
  ) {
    return 'A diagram may not hold click handlers, callbacks or links.';
  }
  const lowered = String(text).toLowerCase();
  if (/javascript\s*:/.test(lowered) || /<\s*\/?\s*[a-z!]/.test(lowered)) return 'A diagram may not hold HTML.';
  const fetching = statements.find((statement) => {
    const styled = statement.toLowerCase();
    return STYLE_STATEMENTS.includes(styled.split(/\s+/)[0]) && (/url\s*\(/.test(styled) || styled.includes('@import'));
  });
  if (fetching) return `A diagram's styles may not fetch anything (url( or @import): ${fetching.slice(0, 80)}`;
  return null;
};

/** What is wrong with a panel before it is sent, or null. */
export const panelProblem = ({ Title, Kind, Content }) => {
  const title = (Title || '').trim();
  if (!title) return 'A panel needs a title.';
  if (title.length > LIMITS.panelTitle) return `A panel title is at most ${LIMITS.panelTitle} characters.`;
  const content = Content || '';
  if (Kind === KIND_MERMAID) {
    if (content.length > LIMITS.mermaid) return `A diagram panel is at most ${LIMITS.mermaid} characters.`;
    return mermaidProblem(content);
  }
  if (content.length > LIMITS.markdown) return `A Markdown panel is at most ${LIMITS.markdown} characters.`;
  return null;
};

/**
 * A line diff of two texts, as [{ type: 'same' | 'added' | 'removed', text }],
 * from the longest common subsequence of their lines. Very long texts are
 * shown as wholly removed and added, which keeps the work bounded.
 */
export const lineDiff = (before, after) => {
  const a = String(before || '').split('\n');
  const b = String(after || '').split('\n');
  if (a.length * b.length > 250000) {
    return [...a.map((text) => ({ type: 'removed', text })), ...b.map((text) => ({ type: 'added', text }))];
  }
  const lengths = Array.from({ length: a.length + 1 }, () => new Array(b.length + 1).fill(0));
  for (let i = a.length - 1; i >= 0; i -= 1) {
    for (let j = b.length - 1; j >= 0; j -= 1) {
      lengths[i][j] = a[i] === b[j] ? lengths[i + 1][j + 1] + 1 : Math.max(lengths[i + 1][j], lengths[i][j + 1]);
    }
  }
  const out = [];
  let i = 0;
  let j = 0;
  while (i < a.length && j < b.length) {
    if (a[i] === b[j]) {
      out.push({ type: 'same', text: a[i] });
      i += 1;
      j += 1;
    } else if (lengths[i + 1][j] >= lengths[i][j + 1]) {
      out.push({ type: 'removed', text: a[i] });
      i += 1;
    } else {
      out.push({ type: 'added', text: b[j] });
      j += 1;
    }
  }
  while (i < a.length) {
    out.push({ type: 'removed', text: a[i] });
    i += 1;
  }
  while (j < b.length) {
    out.push({ type: 'added', text: b[j] });
    j += 1;
  }
  return out;
};

/**
 * Each panel of an earlier version against the current whiteboard, for the
 * history: the panel, whether it is new, gone or changed, and its line diff.
 */
export const compareVersions = (earlier, current) => {
  const now = new Map((current?.Panels || []).map((panel) => [panel.PanelId, panel]));
  const then = new Map((earlier?.Panels || []).map((panel) => [panel.PanelId, panel]));
  const ids = [...then.keys(), ...[...now.keys()].filter((id) => !then.has(id))];
  return ids.map((id) => {
    const old = then.get(id);
    const latest = now.get(id);
    let status = 'same';
    if (!latest) status = 'removed since';
    else if (!old) status = 'added since';
    else if ((old.Content || '') !== (latest.Content || '') || old.Title !== latest.Title) status = 'changed since';
    return {
      panelId: id,
      title: (old || latest).Title,
      status,
      diff: lineDiff(old?.Content || '', latest?.Content || ''),
    };
  });
};

const ERROR_MESSAGES = {
  WhiteboardConflict: 'Someone else changed this whiteboard after you opened it. Reload it to see their change.',
  WhiteboardInvalid: null,
  WhiteboardTooLarge: 'The whiteboard would be larger than 32 KB. Shorten a panel or remove one.',
  WhiteboardLimit: 'This meeting already has as many whiteboards as it may have.',
  WhiteboardNotFound: 'This whiteboard no longer exists.',
  WhiteboardTranscriptDeleted:
    "This meeting's transcript has been deleted, and its whiteboards with it, so they can no longer be changed.",
  WhiteboardRateLimited: 'Too many changes to this meeting in the last minute. Try again shortly.',
};

/** The type of a refused GraphQL request: its errorType, else a Whiteboard… name in its message. */
export const errorType = (error) => {
  const first = (error?.errors || [])[0] || {};
  if (first.errorType) return first.errorType;
  const message = first.message || error?.message || '';
  if (/unauthorized/i.test(message)) return 'Unauthorized';
  const named = message.match(/\b(Whiteboard[A-Z][A-Za-z]+)\b/);
  return named ? named[1] : null;
};

/** A refused request in words. The server's own message names the limit an invalid change broke. */
export const errorMessage = (error) => {
  const type = errorType(error);
  if (type === 'Unauthorized' || type === 'Unauthorized:Forbidden') {
    return 'You do not have permission to change this meeting.';
  }
  const message = (error?.errors || []).map((e) => e?.message).find(Boolean) || error?.message || '';
  return ERROR_MESSAGES[type] || message || 'The change could not be saved. Please try again.';
};

export const isConflict = (error) => errorType(error) === 'WhiteboardConflict';
