/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Adding a piece of text to a whiteboard -- an assistant answer pinned from the
 * chat, or a guidance card -- without React: the change it sends, and a title
 * for a new panel. The server applies the whiteboard rules (rules.py) to the
 * change; panelProblem only saves a round trip.
 */
import { KIND_MARKDOWN, LIMITS, SOURCE_ASSISTANT, panelInputs, panelProblem } from './whiteboard-model';

export const NEW_WHITEBOARD = '__new_whiteboard__';
export const NEW_PANEL = '__new_panel__';

/** A short title from the first line of a text: no Markdown marks, at most `limit` characters. */
export const titleFrom = (text, limit = LIMITS.panelTitle) => {
  const first =
    String(text || '')
      .split('\n')
      .map((line) =>
        line
          .replace(/^[\s#>*\-+\d.)]+/, '')
          .replace(/[*_`~]/g, '')
          .trim(),
      )
      .find(Boolean) || '';
  if (first.length <= limit) return first;
  const cut = first.slice(0, limit - 1);
  const space = cut.lastIndexOf(' ');
  return `${(space > limit / 2 ? cut.slice(0, space) : cut).trimEnd()}…`;
};

/** The Markdown panels text can be added to. A diagram panel holds Mermaid only. */
export const markdownPanels = (board) => (board?.Panels || []).filter((panel) => panel.Kind !== 'mermaid');

/**
 * The whole new panel list of `board` with `text` added: at the end of the panel
 * `panelId`, or as a new panel titled `newTitle`. `keepWithAssistant` keeps an
 * assistant's panel the assistant's (its updates may then reword it); otherwise
 * the changed panel becomes the user's. Returns { panels } or { problem }.
 */
export const panelsWithText = (board, { panelId, newTitle, text, keepWithAssistant }) => {
  const content = String(text || '').trim();
  if (!content) return { problem: 'There is nothing to add.' };
  const current = board?.Panels || [];
  if (panelId === NEW_PANEL || !panelId) {
    if (current.length >= LIMITS.panels) {
      return { problem: `This whiteboard already has ${LIMITS.panels} panels. Add to one of them instead.` };
    }
    const added = { Title: (newTitle || '').trim(), Kind: KIND_MARKDOWN, Content: content };
    const problem = panelProblem(added);
    return problem ? { problem } : { panels: [...panelInputs(current), added] };
  }
  const target = current.find((panel) => panel.PanelId === panelId);
  if (!target) return { problem: 'That panel is no longer on this whiteboard.' };
  const existing = (target.Content || '').replace(/\s+$/, '');
  const combined = existing ? `${existing}\n\n${content}` : content;
  const problem = panelProblem({ Title: target.Title, Kind: target.Kind, Content: combined });
  if (problem) return { problem };
  const keep = keepWithAssistant && target.Source === SOURCE_ASSISTANT;
  return {
    panels: panelInputs(current).map((panel) =>
      panel.PanelId === panelId
        ? { ...panel, Content: combined, ...(keep ? { Source: SOURCE_ASSISTANT } : {}) }
        : panel,
    ),
  };
};

/** A new whiteboard holding `text` in one panel. */
export const newWhiteboardInput = ({ title, panelTitle, text }) => ({
  Title: (title || '').trim() || 'Pinned',
  Template: 'blank',
  Panels: [{ Title: (panelTitle || '').trim() || 'Notes', Kind: KIND_MARKDOWN, Content: String(text || '').trim() }],
});
