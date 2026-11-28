/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The blocks the meeting pack's PDFs add to the layout (pdf-export.js):
 * tables (no clipped cell, the header again on each page), code, the cover's
 * contents with page numbers, the footer, text in other scripts drawn as
 * pictures, and pages that turn landscape for what is wide.
 */
import { afterEach, describe, it, expect, vi } from 'vitest';
import { jsPDF } from 'jspdf';

import { buildPdf, needsRasterText } from './pdf-export';
import { isRightToLeft, textTokens, wrapRuns } from './text-raster';

const pdfStrings = (doc) => [...doc.output().matchAll(/\(((?:\\.|[^\\)])*)\) Tj/g)].map((m) => m[1]);
const pageStrings = (doc, page) => {
  const stream = doc.internal.pages[page].join('\n');
  return [...stream.matchAll(/\(((?:\\.|[^\\)])*)\) Tj/g)].map((m) => m[1]);
};

afterEach(() => vi.restoreAllMocks());

describe('tables', () => {
  const rows = Array.from({ length: 80 }, (_, i) => [
    [{ text: `Risk ${i}` }],
    [{ text: 'High' }],
    [{ text: 'word '.repeat(30) }],
  ]);
  const table = { kind: 'table', header: [[{ text: 'Risk' }], [{ text: 'Impact' }], [{ text: 'Mitigation' }]], rows };

  it('writes every cell, repeats the header on each page, and never runs past the column', async () => {
    const doc = await buildPdf([table], { jsPDF, compress: false });
    expect(doc.getNumberOfPages()).toBeGreaterThan(2);
    for (let page = 1; page <= doc.getNumberOfPages(); page += 1) {
      expect(pageStrings(doc, page)).toContain('Mitigation');
    }
    const strings = pdfStrings(doc);
    rows.forEach((_, i) => expect(strings).toContain(`Risk ${i}`));
    doc.setFont('helvetica', 'normal');
    doc.setFontSize(9);
    strings.filter((s) => s.startsWith('word')).forEach((s) => expect(doc.getTextWidth(s)).toBeLessThan(612 - 108));
  });

  it('continues a cell too tall for one page on the next', async () => {
    const tall = { ...table, rows: [[[{ text: 'Only' }], [{ text: 'x' }], [{ text: 'line\n'.repeat(120) }]]] };
    const doc = await buildPdf([tall], { jsPDF, compress: false });
    expect(doc.getNumberOfPages()).toBe(3);
    expect(pdfStrings(doc).filter((s) => s === 'line')).toHaveLength(120);
  });

  it('puts a table with many columns on a landscape page', async () => {
    const wide = { kind: 'table', header: Array.from({ length: 8 }, (_, i) => [{ text: `C${i}` }]), rows: [] };
    const doc = await buildPdf([{ kind: 'paragraph', runs: [{ text: 'Before' }] }, wide], { jsPDF, compress: false });
    expect(doc.getNumberOfPages()).toBe(2);
    doc.setPage(2);
    expect(doc.internal.pageSize.getWidth()).toBeGreaterThan(doc.internal.pageSize.getHeight());
  });
});

describe('line breaks', () => {
  it('start a new line within a block, as in a transcript segment holding one', async () => {
    const doc = await buildPdf([{ kind: 'paragraph', runs: [{ text: 'Answer, first line\nSources: the guide' }] }], {
      jsPDF,
      compress: false,
    });
    expect(pdfStrings(doc)).toEqual(expect.arrayContaining(['Answer, first line', 'Sources: the guide']));
  });
});

describe('other blocks', () => {
  it('keeps the spaces of code, and wraps a long line inside the block', async () => {
    const doc = await buildPdf([{ kind: 'code', text: `def f():\n    return 1  # ${'x'.repeat(200)}` }], {
      jsPDF,
      compress: false,
    });
    const strings = pdfStrings(doc);
    expect(strings).toContain(
      '    return 1  # xxxxxxxx'.slice(0, 18) + strings.find((s) => s.startsWith('    return')).slice(18),
    );
    expect(strings.filter((s) => /^x+$/.test(s)).length).toBeGreaterThan(0);
  });

  it('writes the contents with the page each heading ended up on, and a footer on every page', async () => {
    const blocks = [
      {
        kind: 'toc',
        entries: [
          { text: 'First', anchor: 'a' },
          { text: 'Second', anchor: 'b' },
        ],
      },
      { kind: 'pageBreak' },
      { kind: 'heading', level: 1, anchor: 'a', runs: [{ text: 'First' }] },
      { kind: 'pageBreak' },
      { kind: 'pageBreak' },
      { kind: 'heading', level: 1, anchor: 'b', runs: [{ text: 'Second' }] },
    ];
    const anchors = {};
    const doc = await buildPdf(blocks, { jsPDF, compress: false, anchors, footer: 'Weekly sync  ·  Whiteboards' });
    expect(anchors).toEqual({ a: 2, b: 3 });
    expect(pageStrings(doc, 1)).toEqual(expect.arrayContaining(['First', 'Second', '2', '3', 'Page 1 of 3']));
    expect(pageStrings(doc, 3)).toContain('Weekly sync  ·  Whiteboards');
  });

  it('draws a task list item with a box, ticked when it is done', async () => {
    const doc = await buildPdf(
      [
        { kind: 'bullet', level: 0, marker: { task: true, checked: true }, runs: [{ text: 'Done' }] },
        { kind: 'bullet', level: 0, marker: { task: true, checked: false }, runs: [{ text: 'Open' }] },
      ],
      { jsPDF, compress: false },
    );
    const stream = doc.internal.pages[1].join('\n');
    // Two boxes (re), and the tick's two strokes (l) in the first.
    expect(stream.match(/ re\b/g)).toHaveLength(2);
    expect(stream.match(/ l\b/g)).toHaveLength(2);
    expect(pageStrings(doc, 1)).toEqual(expect.arrayContaining(['Done', 'Open']));
  });
});

describe('text in other scripts', () => {
  it('tells which text the standard fonts cannot write', () => {
    expect(needsRasterText('Café — 5 €')).toBe(false);
    expect(needsRasterText('会議')).toBe(true);
    expect(needsRasterText('Łódź')).toBe(true);
    expect(needsRasterText(`a${String.fromCharCode(0x200b)}b`)).toBe(false);
  });

  it('draws such a block as pictures of its lines, and other blocks as text', async () => {
    const picture = { data: new Uint8Array([1]), width: 100, height: 14 };
    const rasterizeText = vi.fn(() => [picture, picture]);
    const addImage = vi.spyOn(jsPDF.API, 'addImage').mockImplementation(function addImage() {
      return this;
    });
    const doc = await buildPdf(
      [
        { kind: 'paragraph', runs: [{ text: 'チームの協力' }] },
        { kind: 'paragraph', runs: [{ text: 'Plain text' }] },
      ],
      { jsPDF, compress: false, rasterizeText },
    );
    expect(rasterizeText).toHaveBeenCalledTimes(1);
    expect(rasterizeText.mock.calls[0][0]).toEqual([{ text: 'チームの協力' }]);
    expect(addImage).toHaveBeenCalledTimes(2);
    expect(addImage.mock.calls[0][1]).toBe('PNG');
    expect(pdfStrings(doc)).toContain('Plain text');
  });

  it('breaks a script written without spaces between characters, and keeps a full stop with its word', () => {
    expect(textTokens('会議です。OK fine')).toEqual(['会', '議', 'で', 'す。', 'OK', ' ', 'fine']);
    const lines = wrapRuns([{ text: '会議会議会議' }], 2, (text) => Array.from(text).length);
    expect(lines.map((line) => line.map((piece) => piece.text).join(''))).toEqual(['会議', '会議', '会議']);
  });

  it('reads a line as right to left from its first letter', () => {
    expect(isRightToLeft('العمل الجماعي')).toBe(true);
    expect(isRightToLeft('- 1. שלום')).toBe(true);
    expect(isRightToLeft('Hello العمل')).toBe(false);
  });
});
