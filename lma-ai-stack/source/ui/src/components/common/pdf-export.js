/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The PDF downloads: a document's blocks (markdownToDocBlocks, plus pictures
 * of key frames) laid out as text on PDF pages with jsPDF, so the text can be
 * selected and searched. The jsPDF constructor is passed in: the caller loads
 * the library only when a PDF is asked for, so it is not in the page's bundle.
 *
 * The text is set in Helvetica, one of the fonts every PDF reader has, so no
 * font is embedded. Those fonts hold the Western European characters
 * (Windows-1252); any other character is written as its closest unaccented
 * form, or as "?" when it has none (pdfSafeText).
 *
 * The document is built in steps, with a pause for the browser every
 * `yieldEvery` blocks, so a transcript of thousands of segments does not stop
 * the page from responding while it is built.
 */

const FONT = 'helvetica';
const MARGIN = 54; // points: three quarters of an inch
const BODY_SIZE = 10.5;
const LINE_HEIGHT = 1.35;
const HEADING_SIZES = { 1: 18, 2: 14, 3: 12 };
const BULLET_INDENT = 14;
const BULLET_GAP = 10;
const PARAGRAPH_AFTER = 5;
const BULLET_AFTER = 2;
const IMAGE_AFTER = 8;
// A picture's size on the page: a pixel is three quarters of a point (96 to 72 per inch).
const POINTS_PER_PIXEL = 0.75;
const TEXT_COLOR = '#000000';
const FOOTER_COLOR = '#666666';

// The characters Windows-1252 holds beyond Latin-1, which jsPDF writes in that
// encoding for its standard fonts.
const WINDOWS_1252_EXTRA = new Set('€‚ƒ„…†‡ˆ‰Š‹ŒŽ‘’“”•–—˜™š›œžŸ');
const isWritable = (ch) => {
  const code = ch.codePointAt(0);
  return (code >= 0x20 && code < 0x7f) || (code >= 0xa0 && code <= 0xff) || WINDOWS_1252_EXTRA.has(ch);
};
const ZERO_WIDTH = /[\u200b-\u200d\u2060\ufeff]/;
const COMBINING_MARKS = /[\u0300-\u036f]/g;

const writableChar = (ch) => {
  if (isWritable(ch)) return ch;
  if (ZERO_WIDTH.test(ch)) return '';
  if (/\s/.test(ch)) return ' ';
  const plain = ch.normalize('NFKD').replace(COMBINING_MARKS, '');
  return plain && Array.from(plain).every(isWritable) ? plain : '?';
};

/**
 * Text as the PDF's standard fonts can write it: a character those fonts do
 * not hold becomes its unaccented form ("ő" is "o"), a space, nothing (for a
 * zero-width character), or "?".
 */
export const pdfSafeText = (text) => {
  const value = String(text ?? '');
  if (/^[\x20-\x7e]*$/.test(value)) return value;
  return Array.from(value.normalize('NFC'), writableChar).join('');
};

const pause = () =>
  new Promise((resolve) => {
    setTimeout(resolve, 0);
  });

/**
 * Lays out `blocks` on the pages of a new jsPDF document and returns it. A
 * block is { kind: 'heading', level, runs }, { kind: 'bullet', level, runs },
 * { kind: 'paragraph', runs } or { kind: 'image', image: { data, width,
 * height }, id }; a run is { text, bold, color }, color as 'rrggbb' (as in docx).
 */
export const buildPdf = async (
  blocks,
  { jsPDF, title = '', compress = true, yieldEvery = 100, format = 'letter' } = {},
) => {
  // eslint-disable-next-line new-cap -- jsPDF is the library's own name for its constructor
  const doc = new jsPDF({ unit: 'pt', format, compress });
  if (title) doc.setProperties({ title: pdfSafeText(title) });
  const pageWidth = doc.internal.pageSize.getWidth();
  const pageHeight = doc.internal.pageSize.getHeight();
  const contentWidth = pageWidth - 2 * MARGIN;
  const bottom = pageHeight - MARGIN;
  let y = MARGIN;

  // The font is set only when it changes: measuring thousands of words would
  // otherwise spend most of its time switching fonts.
  let current = '';
  const useFont = (bold, size) => {
    const key = `${bold ? 'b' : 'n'}${size}`;
    if (key === current) return;
    doc.setFont(FONT, bold ? 'bold' : 'normal');
    doc.setFontSize(size);
    current = key;
  };
  const measure = (text, bold, size) => {
    useFont(bold, size);
    return doc.getTextWidth(text);
  };

  const room = (height) => {
    if (y + height > bottom && y > MARGIN) {
      doc.addPage();
      y = MARGIN;
    }
  };

  // Runs as lines no wider than `width`: each line a list of pieces
  // { text, bold, color, width }. A word wider than the line is broken.
  const layoutLines = (runs, width, size, forceBold) => {
    const lines = [[]];
    let lineWidth = 0;
    const newLine = () => {
      const line = lines[lines.length - 1];
      while (line.length && !line[line.length - 1].text.trim()) lineWidth -= line.pop().width;
      lines.push([]);
      lineWidth = 0;
    };
    const place = (text, bold, color) => {
      const w = measure(text, bold, size);
      lines[lines.length - 1].push({ text, bold, color, width: w });
      lineWidth += w;
    };
    runs.forEach((run) => {
      const bold = Boolean(forceBold || run.bold);
      const color = run.color ? `#${run.color}` : TEXT_COLOR;
      pdfSafeText(String(run.text ?? '').replace(/\r\n?/g, '\n'))
        .split(/(\n|[ \t]+)/)
        .filter((token) => token !== '')
        .forEach((token) => {
          if (token === '\n') {
            newLine();
            return;
          }
          if (!token.trim()) {
            if (lines[lines.length - 1].length) place(' ', bold, color);
            return;
          }
          let word = token;
          let w = measure(word, bold, size);
          if (lineWidth + w > width && lines[lines.length - 1].length) {
            newLine();
          }
          // A word wider than a whole line: as many characters as fit, line by line.
          while (w > width && word.length > 1) {
            let end = word.length - 1;
            while (end > 1 && measure(word.slice(0, end), bold, size) > width) end -= 1;
            place(word.slice(0, end), bold, color);
            newLine();
            word = word.slice(end);
            w = measure(word, bold, size);
          }
          place(word, bold, color);
        });
    });
    newLine();
    lines.pop();
    return lines.length ? lines : [[]];
  };

  const drawLine = (pieces, x, size) => {
    let cursor = x;
    // Neighbouring pieces in one style are written as one string, so the
    // text copies out of the PDF as it was written.
    const groups = [];
    pieces.forEach((piece) => {
      const last = groups[groups.length - 1];
      if (last && last.bold === piece.bold && last.color === piece.color) {
        last.text += piece.text;
        last.width += piece.width;
      } else groups.push({ ...piece });
    });
    groups.forEach((group) => {
      useFont(group.bold, size);
      doc.setTextColor(group.color);
      doc.text(group.text, cursor, y, { baseline: 'top' });
      cursor += group.width;
    });
  };

  const writeRuns = (runs, { x, width, size, bold = false, after = PARAGRAPH_AFTER, marker = null }) => {
    const lineHeight = size * LINE_HEIGHT;
    const lines = layoutLines(runs, width, size, bold);
    lines.forEach((line, index) => {
      room(lineHeight);
      if (index === 0 && marker) {
        useFont(false, size);
        doc.setTextColor(TEXT_COLOR);
        doc.text(marker.text, marker.x, y, { baseline: 'top' });
      }
      drawLine(line, x, size);
      y += lineHeight;
    });
    y += after;
  };

  const writeImage = (block) => {
    const { data, width, height } = block.image;
    let w = Math.min(contentWidth, width * POINTS_PER_PIXEL);
    let h = (height / width) * w;
    const maxHeight = bottom - MARGIN;
    if (h > maxHeight) {
      w *= maxHeight / h;
      h = maxHeight;
    }
    room(h);
    doc.addImage(data, 'JPEG', MARGIN, y, w, h, block.id ? `keyframe-${block.id}` : undefined, 'NONE');
    y += h + IMAGE_AFTER;
  };

  for (let i = 0; i < blocks.length; i += 1) {
    if (i > 0 && i % yieldEvery === 0) {
      // eslint-disable-next-line no-await-in-loop -- the pause is the point: it lets the page respond
      await pause();
    }
    const block = blocks[i];
    if (block.kind === 'heading') {
      const size = HEADING_SIZES[block.level] || HEADING_SIZES[3];
      if (y > MARGIN) y += size * 0.5;
      // A heading is kept with the line after it.
      room(size * LINE_HEIGHT + BODY_SIZE * LINE_HEIGHT * 2);
      writeRuns(block.runs, { x: MARGIN, width: contentWidth, size, bold: true, after: size * 0.4 });
    } else if (block.kind === 'bullet') {
      const indent = MARGIN + BULLET_INDENT * (block.level || 0);
      writeRuns(block.runs, {
        x: indent + BULLET_GAP,
        width: contentWidth - (indent - MARGIN) - BULLET_GAP,
        size: BODY_SIZE,
        after: BULLET_AFTER,
        marker: { text: '•', x: indent },
      });
    } else if (block.kind === 'image' && block.image) {
      writeImage(block);
    } else if (block.runs) {
      writeRuns(block.runs, { x: MARGIN, width: contentWidth, size: BODY_SIZE });
    }
  }

  // "Page 2 of 7" at the foot of each page.
  const pages = doc.getNumberOfPages();
  for (let page = 1; page <= pages; page += 1) {
    doc.setPage(page);
    useFont(false, 8);
    doc.setTextColor(FOOTER_COLOR);
    doc.text(`Page ${page} of ${pages}`, pageWidth - MARGIN, pageHeight - MARGIN / 2, { align: 'right' });
  }
  return doc;
};
