/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The Whiteboards PDF: a panel drawn by its kind (a diagram, or Markdown
 * through the page's renderer), a diagram that cannot be drawn written as its
 * source, and the document's cover, contents and sections.
 */
import { describe, it, expect, vi } from 'vitest';
import { jsPDF } from 'jspdf';

import {
  DIAGRAM_NOT_DRAWN,
  buildWhiteboardsPdf,
  isWideDiagram,
  markdownPdfBlocks,
  panelBlocks,
  svgSize,
  whiteboardsPdfBlocks,
} from './whiteboard-pdf';

const text = (block) => (block.runs || []).map((run) => run.text).join('');
const PICTURE = { data: new Uint8Array([1, 2]), width: 400, height: 300, format: 'PNG' };

describe('markdownPdfBlocks', () => {
  it('lays out headings, paragraphs with emphasis, lists and task lists', () => {
    const blocks = markdownPdfBlocks(
      '## Plan\n\nWe **agreed** the *scope*, see [the guide](https://example.com/g).\n\n- One\n  - Nested\n- [x] Done\n- [ ] Open\n\n1. First\n2. Second',
    );
    expect(blocks[0]).toMatchObject({ kind: 'heading', level: 3, size: 12 });
    expect(text(blocks[0])).toBe('Plan');
    expect(blocks[1].runs).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ text: 'agreed', bold: true }),
        expect.objectContaining({ text: 'scope', italic: true }),
        expect.objectContaining({ text: ' (https://example.com/g)' }),
      ]),
    );
    const bullets = blocks.filter((block) => block.kind === 'bullet');
    expect(bullets.map((b) => [text(b), b.level])).toEqual([
      ['One', 0],
      ['Nested', 1],
      ['Done', 0],
      ['Open', 0],
      ['First', 0],
      ['Second', 0],
    ]);
    expect(bullets[2].marker).toEqual({ task: true, checked: true });
    expect(bullets[3].marker).toEqual({ task: true, checked: false });
    expect(bullets[5].marker).toBe('2.');
  });

  it('reads a table with its header, its rows and each column alignment', () => {
    const [table] = markdownPdfBlocks('| Risk | Impact |\n|---|:---:|\n| Slip | **High** |\n| Late | Low |');
    expect(table.kind).toBe('table');
    expect(table.header.map((cell) => cell.map((run) => run.text).join(''))).toEqual(['Risk', 'Impact']);
    expect(table.rows).toHaveLength(2);
    expect(table.rows[0][1]).toEqual([expect.objectContaining({ text: 'High', bold: true })]);
    expect(table.align).toEqual(['left', 'center']);
  });

  it('keeps code as it was written, and a quote as a quote', () => {
    const blocks = markdownPdfBlocks('```\nif x:\n    y()\n```\n\n> Moves if review slips.');
    expect(blocks[0]).toEqual({ kind: 'code', text: 'if x:\n    y()' });
    expect(blocks[1]).toMatchObject({ kind: 'quote' });
    expect(text(blocks[1])).toBe('Moves if review slips.');
  });

  it('writes HTML in the text as the text it is, and no picture', () => {
    const blocks = markdownPdfBlocks('<b>bold?</b> and ![x](https://example.com/p.png)');
    expect(text(blocks[0])).toContain('<b>bold?</b>');
    expect(JSON.stringify(blocks)).not.toContain('p.png');
  });
});

describe('panelBlocks', () => {
  it('draws a diagram panel, and centres the picture', async () => {
    const draw = vi.fn(async () => PICTURE);
    const blocks = await panelBlocks({ Kind: 'mermaid', Content: 'flowchart LR\n a --> b' }, { draw });
    expect(draw).toHaveBeenCalledWith('flowchart LR\n a --> b');
    expect(blocks).toEqual([{ kind: 'image', image: PICTURE, center: true, landscapeIfWide: true }]);
  });

  it('writes a diagram that cannot be drawn as its source, under the note the page shows', async () => {
    const failing = async () => {
      throw new Error('Parse error');
    };
    const blocks = await panelBlocks({ Kind: 'mermaid', Content: 'flowchart TD\n a -->' }, { draw: failing });
    expect(text(blocks[0])).toBe(DIAGRAM_NOT_DRAWN);
    expect(blocks[1]).toEqual({ kind: 'code', text: 'flowchart TD\n a -->' });
  });

  it('does not draw a diagram outside the subset a panel may hold, and says why', async () => {
    const draw = vi.fn();
    const blocks = await panelBlocks({ Kind: 'mermaid', Content: 'pie\n "a" : 1' }, { draw });
    expect(draw).not.toHaveBeenCalled();
    expect(text(blocks[0])).toMatch(/must start with one of/);
    expect(blocks[1].kind).toBe('code');
  });

  it('lays out a panel of any other kind as Markdown, and an empty one as the page says it', async () => {
    expect(text((await panelBlocks({ Kind: 'something-new', Content: 'Hello' }))[0])).toBe('Hello');
    expect(text((await panelBlocks({ Kind: 'markdown', Content: '  ' }))[0])).toBe('Nothing here yet.');
    expect(text((await panelBlocks({ Kind: 'mermaid', Content: '' }))[0])).toBe('No diagram yet.');
  });
});

describe('the document', () => {
  const BOARDS = [
    {
      WhiteboardId: 'w1',
      Title: 'Risks',
      Template: 'decisions',
      Version: 3,
      UpdatedBy: 'ana@example.com',
      Panels: [
        { PanelId: 'p1', Title: 'Diagram', Kind: 'mermaid', Content: 'flowchart LR\n a --> b', Source: 'assistant' },
        { PanelId: 'p2', Title: 'Notes', Kind: 'markdown', Content: 'Keep **this**.', UpdatedBy: 'bo@example.com' },
      ],
    },
    { WhiteboardId: 'w2', Title: 'Empty', Panels: [] },
  ];
  const meeting = { title: 'Weekly sync', facts: [{ label: 'Owner', value: 'ana@example.com' }], whiteboards: BOARDS };

  it('opens with a cover listing each whiteboard, then gives each its own section, panels in order', async () => {
    const onProgress = vi.fn();
    const blocks = await whiteboardsPdfBlocks(meeting, { draw: async () => PICTURE, onProgress });
    const toc = blocks.find((block) => block.kind === 'toc');
    expect(toc.entries).toEqual([
      { text: 'Risks', anchor: 'wb-0' },
      { text: 'Empty', anchor: 'wb-1' },
    ]);
    const headings = blocks.filter((block) => block.kind === 'heading').map((block) => [block.level, text(block)]);
    expect(headings).toEqual([
      [0, 'Weekly sync'],
      [2, 'Contents'],
      [1, 'Risks'],
      [2, 'Diagram'],
      [2, 'Notes'],
      [1, 'Empty'],
    ]);
    expect(blocks.filter((block) => block.kind === 'pageBreak')).toHaveLength(2);
    const notes = blocks.map(text);
    expect(notes).toContain('Assistant');
    expect(notes).toContain('Edited by bo@example.com');
    expect(notes).toContain('This whiteboard has no panels.');
    expect(notes.some((line) => line.startsWith('Template: Decisions and open questions'))).toBe(true);
    expect(onProgress).toHaveBeenLastCalledWith(2, 2);
  });

  it('puts a wide diagram on a landscape page with its title, the whiteboard too when it opens with one', async () => {
    const wide = { ...PICTURE, width: 1600, height: 500 };
    expect(isWideDiagram(wide)).toBe(true);
    expect(isWideDiagram(PICTURE)).toBe(false);
    const draw = async () => wide;
    const at = (blocks, title) => blocks.findIndex((block) => block.kind === 'heading' && text(block) === title);
    const first = await whiteboardsPdfBlocks(meeting, { draw });
    expect(first[at(first, 'Risks') - 1]).toEqual({ kind: 'pageBreak', orientation: 'landscape' });
    expect(first[at(first, 'Diagram') - 1].kind).not.toBe('pageBreak');
    const [board] = BOARDS;
    const later = { ...meeting, whiteboards: [{ ...board, Panels: [...board.Panels].reverse() }] };
    const second = await whiteboardsPdfBlocks(later, { draw });
    expect(second[at(second, 'Risks') - 1]).toEqual({ kind: 'pageBreak' });
    expect(second[at(second, 'Diagram') - 1]).toEqual({ kind: 'pageBreak', orientation: 'landscape' });
  });

  it('builds a PDF whose text can be selected, with the contents page numbers', async () => {
    const addImage = vi.spyOn(jsPDF.API, 'addImage').mockImplementation(function addImage() {
      return this;
    });
    const doc = await buildWhiteboardsPdf(meeting, { jsPDF, compress: false, draw: async () => PICTURE });
    const strings = [...doc.output().matchAll(/\(((?:\\.|[^\\)])*)\) Tj/g)].map((m) => m[1]);
    expect(strings).toEqual(expect.arrayContaining(['WHITEBOARDS', 'Weekly sync', 'Risks', 'Notes', 'Keep ', 'this', '2', '3']));
    expect(strings).toContain(`Page 1 of ${doc.getNumberOfPages()}`);
    expect(addImage).toHaveBeenCalledTimes(1);
    addImage.mockRestore();
  });
});

describe('svgSize', () => {
  const svg = (attrs) => new DOMParser().parseFromString(`<svg xmlns="http://www.w3.org/2000/svg" ${attrs}/>`, 'image/svg+xml').documentElement;

  it("reads a drawing's size from its viewBox, else its width and height", () => {
    expect(svgSize(svg('viewBox="-8 -8 640.5 300" width="100%"'))).toEqual({ width: 640.5, height: 300 });
    expect(svgSize(svg('width="200" height="100"'))).toEqual({ width: 200, height: 100 });
    expect(svgSize(svg('width="100%"'))).toBeNull();
  });
});
