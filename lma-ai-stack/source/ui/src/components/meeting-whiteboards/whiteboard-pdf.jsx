/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The Whiteboards PDF: every whiteboard of a meeting as the Whiteboards tab
 * shows it -- its title, its panels in order, each with its title, who it
 * belongs to and when it changed, and its content -- one whiteboard per
 * section, after a cover with the meeting's name, date and owner and a list
 * of the whiteboards with their pages. See docs/meeting-whiteboards.md.
 *
 * What a panel holds is drawn by its kind, not by the template it came from,
 * so every template draws, including ones added later:
 * - a diagram panel (Kind `mermaid`) is drawn by the page's own Mermaid
 *   renderer (loadMermaid in MermaidDiagram.jsx: strict security level, the
 *   SVG sanitized with DOMPurify), and the drawing is placed in the PDF as a
 *   picture at two and a half times its size. A diagram outside the subset a
 *   panel may hold, or one that cannot be drawn, is written as its source
 *   under the same note the page shows. A wide diagram (a Gantt chart) that
 *   would be shrunk on a portrait page gets a landscape page.
 * - any other panel is Markdown, rendered by the page's own renderer
 *   (WhiteboardMarkdown: raw HTML escaped, no pictures) and its headings,
 *   paragraphs, lists, task lists, tables, quotes and code laid out as text
 *   (pdf-export.js). A table too wide for a portrait page gets a landscape
 *   page, and a row too tall for what is left of one continues on the next,
 *   under the table's header again.
 *
 * Loaded on demand (meeting-pack.js), so react-dom/server, Mermaid and
 * jsPDF are not in the page's bundle.
 */
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';

import WhiteboardMarkdown from './WhiteboardMarkdown';
import { loadMermaid } from './MermaidDiagram';
import { KIND_MERMAID, SOURCE_ASSISTANT, TEMPLATES, mermaidProblem } from './whiteboard-model';
import { buildPdf } from '../common/pdf-export';
import { coverBlocks } from '../common/pdf-cover';

const GREY = '5f6b7a';
const LINK = '0b5cad';
const META_SIZE = 8.5;
const SAFE_LINK = /^(?:https?:\/\/|mailto:)/i;
// The largest picture a diagram is drawn as, in pixels, to stay inside every
// browser's canvas limits.
const MAX_DIAGRAM_PIXELS = 24_000_000;
const MAX_DIAGRAM_SIDE = 8000;
export const DIAGRAM_SCALE = 2.5;
export const DIAGRAM_NOT_DRAWN = 'This diagram could not be drawn; its source is shown.';

const plain = (text, extra = {}) => ({ text: String(text ?? ''), ...extra });

/** A panel's Markdown as the page renders it (WhiteboardMarkdown), as an HTML string. */
export const whiteboardMarkdownHtml = (markdown) =>
  renderToStaticMarkup(<WhiteboardMarkdown>{String(markdown ?? '')}</WhiteboardMarkdown>);

// The runs of an element's inline content.
const inlineRuns = (node, style = {}, out = []) => {
  node.childNodes.forEach((child) => {
    if (child.nodeType === 3) {
      const text = child.nodeValue.replace(/\s+/g, ' ');
      if (text) out.push({ text, ...style });
      return;
    }
    if (child.nodeType !== 1) return;
    const tag = child.tagName.toLowerCase();
    if (tag === 'br') out.push({ text: '\n', ...style });
    else if (tag === 'strong' || tag === 'b') inlineRuns(child, { ...style, bold: true }, out);
    else if (tag === 'em' || tag === 'i') inlineRuns(child, { ...style, italic: true }, out);
    else if (tag === 'del' || tag === 's') inlineRuns(child, { ...style, strike: true }, out);
    else if (tag === 'code') out.push({ text: child.textContent, ...style, mono: true });
    else if (tag === 'a') {
      const href = child.getAttribute('href') || '';
      const label = child.textContent;
      inlineRuns(child, { ...style, color: LINK }, out);
      const shown = href.replace(/^mailto:/i, '');
      if (SAFE_LINK.test(href) && shown !== label.trim()) out.push({ text: ` (${shown})`, ...style, color: GREY });
    } else if (tag === 'input' || tag === 'ul' || tag === 'ol' || tag === 'table' || tag === 'pre') {
      // A checkbox is the item's marker; nested blocks are laid out on their own.
    } else inlineRuns(child, style, out);
  });
  return out;
};

// The runs without the spaces at either end, as HTML shows a block.
const trimRuns = (runs) => {
  const out = runs.filter((run) => run.text !== '');
  while (out.length && !out[0].text.trim()) out.shift();
  while (out.length && !out[out.length - 1].text.trim()) out.pop();
  if (out.length) out[0] = { ...out[0], text: out[0].text.replace(/^ +/, '') };
  if (out.length) out[out.length - 1] = { ...out[out.length - 1], text: out[out.length - 1].text.replace(/ +$/, '') };
  return out;
};

const cellAlign = (cell) => {
  const align = (cell.getAttribute('align') || cell.style?.textAlign || '').toLowerCase();
  return ['center', 'right'].includes(align) ? align : 'left';
};

const tableBlock = (table) => {
  const rowsOf = (section) => [...(section?.querySelectorAll(':scope > tr') || [])];
  const head = rowsOf(table.querySelector(':scope > thead'));
  const body = [...rowsOf(table.querySelector(':scope > tbody')), ...rowsOf(table)];
  const cells = (tr) => [...tr.children].filter((c) => /^t[hd]$/i.test(c.tagName));
  const headerCells = head.length ? cells(head[0]) : [];
  return {
    kind: 'table',
    header: headerCells.map((c) => trimRuns(inlineRuns(c))),
    rows: body.map((tr) => cells(tr).map((c) => trimRuns(inlineRuns(c)))),
    align: (headerCells.length ? headerCells : cells(body[0] || table)).map(cellAlign),
  };
};

const listBlocks = (list, level, out) => {
  const ordered = list.tagName.toLowerCase() === 'ol';
  let number = Number(list.getAttribute('start')) || 1;
  [...list.children].forEach((item) => {
    if (item.tagName.toLowerCase() !== 'li') return;
    const box = item.querySelector(':scope > input[type="checkbox"], :scope > p > input[type="checkbox"]');
    let marker = '•';
    if (box) marker = { task: true, checked: box.hasAttribute('checked') };
    else if (ordered) marker = `${number}.`;
    number += 1;
    // A loose list item holds paragraphs; their text is the item's.
    const runs = [];
    item.childNodes.forEach((child) => {
      if (child.nodeType === 1 && /^(ul|ol)$/i.test(child.tagName)) return;
      if (child.nodeType === 1 && child.tagName.toLowerCase() === 'p') {
        if (runs.length) runs.push({ text: '\n' });
        inlineRuns(child, {}, runs);
      } else if (child.nodeType === 3 || (child.nodeType === 1 && !/^(pre|table|blockquote)$/i.test(child.tagName))) {
        const holder = { childNodes: [child] };
        inlineRuns(holder, {}, runs);
      }
    });
    out.push({ kind: 'bullet', level: Math.min(level, 4), marker, runs: trimRuns(runs) });
    item.childNodes.forEach((child) => {
      if (child.nodeType !== 1) return;
      const tag = child.tagName.toLowerCase();
      if (tag === 'ul' || tag === 'ol') listBlocks(child, level + 1, out);
      // eslint-disable-next-line no-use-before-define -- blocks within a list item
      else if (tag === 'pre' || tag === 'table' || tag === 'blockquote') blocksOf(child, out, level + 1);
    });
  });
};

/** The PDF blocks (pdf-export.js) of a rendered panel's HTML. */
const blocksOf = (node, out = [], level = 0) => {
  const tag = node.tagName?.toLowerCase();
  if (/^h[1-6]$/.test(tag)) {
    const runs = trimRuns(inlineRuns(node));
    // Under the panel's own title (14 points): 13, 12 and 11 points, then bold text.
    const depth = Number(tag[1]);
    if (depth <= 3) out.push({ kind: 'heading', level: 3, size: 14 - depth, runs });
    else out.push({ kind: 'paragraph', runs: runs.map((run) => ({ ...run, bold: true })) });
  } else if (tag === 'p') {
    const runs = trimRuns(inlineRuns(node));
    if (runs.length) out.push({ kind: 'paragraph', runs });
  } else if (tag === 'ul' || tag === 'ol') {
    listBlocks(node, level, out);
  } else if (tag === 'table') {
    out.push(tableBlock(node));
  } else if (tag === 'pre') {
    out.push({ kind: 'code', text: node.textContent.replace(/\n$/, '') });
  } else if (tag === 'blockquote') {
    const inner = [];
    [...node.children].forEach((child) => blocksOf(child, inner, level));
    inner.forEach((block) => out.push(block.kind === 'paragraph' ? { ...block, kind: 'quote' } : block));
  } else if (tag === 'hr') {
    out.push({ kind: 'rule' });
  } else {
    node.childNodes.forEach((child) => {
      if (child.nodeType === 1) blocksOf(child, out, level);
      else if (child.nodeType === 3 && child.nodeValue.trim()) {
        out.push({ kind: 'paragraph', runs: [plain(child.nodeValue.trim())] });
      }
    });
  }
  return out;
};

/** A panel's Markdown as PDF blocks, through the page's renderer. */
export const markdownPdfBlocks = (markdown) => {
  const html = whiteboardMarkdownHtml(markdown);
  const parsed = new DOMParser().parseFromString(`<!DOCTYPE html><html><body>${html}</body></html>`, 'text/html');
  return blocksOf(parsed.body);
};

/** The size a drawn diagram's SVG gives itself, in pixels: its viewBox, else its width and height. */
export const svgSize = (svgElement) => {
  const box = (svgElement.getAttribute('viewBox') || '')
    .trim()
    .split(/[\s,]+/)
    .map(Number);
  if (box.length === 4 && box[2] > 0 && box[3] > 0) return { width: box[2], height: box[3] };
  const width = parseFloat(svgElement.getAttribute('width'));
  const height = parseFloat(svgElement.getAttribute('height'));
  return width > 0 && height > 0 ? { width, height } : null;
};

/**
 * A drawn diagram (sanitized SVG markup) as a PNG on white: { data, width,
 * height, format: 'PNG' }, width and height in CSS pixels (the picture itself
 * is DIAGRAM_SCALE times larger, within the canvas limits).
 */
export const svgToPng = async (svg, { scale = DIAGRAM_SCALE } = {}) => {
  const parsed = new DOMParser().parseFromString(svg, 'image/svg+xml');
  const root = parsed.documentElement;
  if (!root || root.tagName.toLowerCase() !== 'svg') throw new Error('not an SVG drawing');
  const size = svgSize(root);
  if (!size) throw new Error('the drawing has no size');
  root.setAttribute('width', String(size.width));
  root.setAttribute('height', String(size.height));
  root.style.removeProperty('max-width');
  if (!root.getAttribute('xmlns')) root.setAttribute('xmlns', 'http://www.w3.org/2000/svg');
  const markup = new XMLSerializer().serializeToString(root);
  const url = URL.createObjectURL(new Blob([markup], { type: 'image/svg+xml;charset=utf-8' }));
  try {
    const image = new Image();
    image.src = url;
    await image.decode();
    const fit = Math.min(
      scale,
      Math.sqrt(MAX_DIAGRAM_PIXELS / (size.width * size.height)),
      MAX_DIAGRAM_SIDE / size.width,
      MAX_DIAGRAM_SIDE / size.height,
    );
    const canvas = document.createElement('canvas');
    canvas.width = Math.max(1, Math.round(size.width * fit));
    canvas.height = Math.max(1, Math.round(size.height * fit));
    const context = canvas.getContext('2d');
    context.fillStyle = '#ffffff';
    context.fillRect(0, 0, canvas.width, canvas.height);
    context.drawImage(image, 0, 0, canvas.width, canvas.height);
    const blob = await new Promise((resolve, reject) => {
      canvas.toBlob((out) => (out ? resolve(out) : reject(new Error('the drawing could not be encoded'))), 'image/png');
    });
    return { data: new Uint8Array(await blob.arrayBuffer()), width: size.width, height: size.height, format: 'PNG' };
  } finally {
    URL.revokeObjectURL(url);
  }
};

// The text column of a portrait letter page, in points (pdf-export.js).
const PORTRAIT_COLUMN = 612 - 2 * 54;

/**
 * Whether a drawn diagram would be shrunk by more than a quarter on a
 * portrait page, and is wider than tall, so that a landscape page shows it larger.
 */
export const isWideDiagram = ({ width, height }) => width > height * 1.3 && width * 0.75 > PORTRAIT_COLUMN * 1.3;

let diagramCount = 0;

/**
 * A diagram's source drawn as the page draws it, then as a picture: the
 * picture ({ data, width, height, format }), or null when it cannot be drawn.
 */
export const drawDiagram = async (source, { renderer = loadMermaid, rasterize = svgToPng } = {}) => {
  diagramCount += 1;
  const { render } = await renderer();
  const svg = await render(`wb-pdf-diagram-${diagramCount}`, source);
  if (!svg) return null;
  return rasterize(svg);
};

const metaParagraph = (text, extra = {}) => ({
  kind: 'paragraph',
  size: META_SIZE,
  after: 3,
  runs: [{ text, color: GREY, ...extra }],
});

const templateLabel = (name) => TEMPLATES.find((template) => template.name === name)?.label || name || '';

/** When something changed, as the Whiteboards tab writes it. */
const localTime = (iso) => {
  if (!iso) return '';
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleString();
};

/** Who a panel belongs to, as the tab's badge says it. */
export const panelOwner = (panel) => {
  if (panel.Source === SOURCE_ASSISTANT) return 'Assistant';
  return panel.UpdatedBy ? `Edited by ${panel.UpdatedBy}` : 'Edited by a user';
};

/** A panel's content as PDF blocks: a diagram's picture (or its source), or its Markdown. */
export const panelBlocks = async (panel, { draw = drawDiagram } = {}) => {
  const content = String(panel.Content || '');
  if (panel.Kind === KIND_MERMAID) {
    const source = content.trim();
    if (!source) return [metaParagraph('No diagram yet.', { italic: true })];
    const problem = mermaidProblem(source);
    let picture = null;
    if (!problem) {
      try {
        picture = await draw(source);
      } catch (error) {
        picture = null;
      }
    }
    if (picture) return [{ kind: 'image', image: picture, center: true, landscapeIfWide: true }];
    return [metaParagraph(problem || DIAGRAM_NOT_DRAWN, { italic: true }), { kind: 'code', text: source }];
  }
  if (!content.trim()) return [metaParagraph('Nothing here yet.', { italic: true })];
  return markdownPdfBlocks(content);
};

/**
 * The document's blocks: the cover, then each whiteboard from a new page.
 * `onProgress(done, total)` is told as each whiteboard is laid out.
 */
export const whiteboardsPdfBlocks = async (
  { title, facts = [], note = '', whiteboards = [] },
  { draw = drawDiagram, onProgress = () => {} } = {},
) => {
  const boards = whiteboards || [];
  const blocks = coverBlocks({
    kind: 'Whiteboards',
    title,
    facts: [...facts, { label: 'Whiteboards', value: String(boards.length) }],
    note,
    contents: boards.map((board, index) => ({ text: board.Title || `Whiteboard ${index + 1}`, anchor: `wb-${index}` })),
  });
  for (let index = 0; index < boards.length; index += 1) {
    const board = boards[index];
    const panels = board.Panels || [];
    const contents = [];
    for (let p = 0; p < panels.length; p += 1) {
      // eslint-disable-next-line no-await-in-loop -- diagrams are drawn one at a time, as Mermaid requires
      contents.push(await panelBlocks(panels[p], { draw }));
    }
    // A diagram too wide for a portrait page starts a landscape one, with its
    // title; a whiteboard that opens with one starts on that landscape page.
    const wide = contents.map((content) => content[0]?.kind === 'image' && isWideDiagram(content[0].image));
    blocks.push(wide[0] ? { kind: 'pageBreak', orientation: 'landscape' } : { kind: 'pageBreak' });
    blocks.push({
      kind: 'heading',
      level: 1,
      anchor: `wb-${index}`,
      runs: [plain(board.Title || `Whiteboard ${index + 1}`)],
    });
    const meta = [
      board.Template ? `Template: ${templateLabel(board.Template)}` : '',
      board.Version ? `Version ${board.Version}` : '',
      board.UpdatedAt ? `updated ${localTime(board.UpdatedAt)}` : '',
      board.UpdatedBy ? `by ${board.UpdatedBy}` : '',
    ].filter(Boolean);
    if (meta.length) blocks.push(metaParagraph(meta.join('  ·  ')));
    if (board.Instructions) blocks.push(metaParagraph(`Instructions: ${board.Instructions}`, { italic: true }));
    blocks.push({ kind: 'rule' });
    if (!panels.length) blocks.push(metaParagraph('This whiteboard has no panels.', { italic: true }));
    panels.forEach((panel, p) => {
      if (wide[p] && p > 0) blocks.push({ kind: 'pageBreak', orientation: 'landscape' });
      blocks.push({ kind: 'heading', level: 2, runs: [plain(panel.Title || 'Panel')] });
      blocks.push(metaParagraph([panelOwner(panel), localTime(panel.UpdatedAt)].filter(Boolean).join('  ·  ')));
      blocks.push(...contents[p]);
    });
    onProgress(index + 1, boards.length);
  }
  return blocks;
};

/**
 * The Whiteboards PDF as a Blob. `jsPDF` is the library's constructor;
 * `rasterizeText` draws text the standard fonts cannot write (text-raster.js).
 */
export const buildWhiteboardsPdf = async (
  meeting,
  { jsPDF, rasterizeText = null, draw = drawDiagram, onProgress = () => {}, compress = true } = {},
) => {
  const blocks = await whiteboardsPdfBlocks(meeting, { draw, onProgress });
  const doc = await buildPdf(blocks, {
    jsPDF,
    compress,
    title: `${meeting.title} - Whiteboards`,
    footer: `${meeting.title}  ·  Whiteboards`,
    rasterizeText,
  });
  return doc;
};
