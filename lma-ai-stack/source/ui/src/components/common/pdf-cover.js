/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The cover page the meeting pack's PDFs open with (the slides and the
 * whiteboards; pdf-export.js lays it out): what the document is, the
 * meeting's name, its facts (date, owner and so on), an optional note, such
 * as that the meeting was still going on, and the contents.
 */

const GREY = '5f6b7a';
const ACCENT = '0972d3';

const factBlock = (label, value) => ({
  kind: 'paragraph',
  after: 3,
  runs: [
    { text: `${label}: `, bold: true },
    { text: value, color: '16191f' },
  ],
});

/**
 * The cover's blocks. `facts` are [{ label, value }] (an empty value is left
 * out); `contents` are [{ text, anchor }], each anchor a heading's, whose
 * page number is written once the document is laid out.
 */
export const coverBlocks = ({ kind, title, facts = [], note = '', contents = [] }) => [
  { kind: 'spacer', height: 110 },
  {
    kind: 'paragraph',
    size: 11,
    after: 6,
    runs: [{ text: String(kind || '').toUpperCase(), bold: true, color: ACCENT }],
  },
  { kind: 'heading', level: 0, runs: [{ text: String(title || 'Meeting') }] },
  { kind: 'rule', color: `#${ACCENT}`, width: 72, weight: 2 },
  { kind: 'spacer', height: 6 },
  ...facts.filter((fact) => fact?.value).map(({ label, value }) => factBlock(label, String(value))),
  ...(note
    ? [
        { kind: 'spacer', height: 6 },
        { kind: 'paragraph', size: 8.5, runs: [{ text: note, color: GREY, italic: true }] },
      ]
    : []),
  ...(contents.length
    ? [
        { kind: 'spacer', height: 24 },
        { kind: 'heading', level: 2, runs: [{ text: 'Contents' }] },
        { kind: 'toc', entries: contents },
      ]
    : []),
];

export default coverBlocks;
