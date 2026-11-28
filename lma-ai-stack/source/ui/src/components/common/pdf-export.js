/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The PDF downloads: a document's blocks (markdownToDocBlocks, plus pictures
 * of key frames, and the tables, code, quotes and diagrams of a whiteboard)
 * laid out as text on PDF pages with jsPDF, so the text can be selected and
 * searched. The jsPDF constructor is passed in: the caller loads the library
 * only when a PDF is asked for, so it is not in the page's bundle.
 *
 * The text is set in Helvetica (Courier for code), fonts every PDF reader has,
 * so no font is embedded. Those fonts hold the Western European characters
 * (Windows-1252). A block holding any other character (another script, an
 * emoji) is drawn as a picture of its text when the caller passes
 * `rasterizeText` (text-raster.js, which draws with the browser's own fonts);
 * without it, such a character is written as its closest unaccented form, or
 * as "?" when it has none (pdfSafeText).
 *
 * The document is built in steps, with a pause for the browser every
 * `yieldEvery` blocks, so a transcript of thousands of segments does not stop
 * the page from responding while it is built.
 */

const FONT = 'helvetica';
const MONO_FONT = 'courier';
const MARGIN = 54; // points: three quarters of an inch
const BODY_SIZE = 10.5;
const LINE_HEIGHT = 1.35;
const HEADING_SIZES = { 0: 24, 1: 18, 2: 14, 3: 12 };
const BULLET_INDENT = 14;
const BULLET_GAP = 10;
const PARAGRAPH_AFTER = 5;
const BULLET_AFTER = 2;
const IMAGE_AFTER = 8;
const TABLE_SIZE = 9;
const CELL_PAD = 4;
const CODE_SIZE = 8.5;
const CODE_PAD = 6;
const QUOTE_INDENT = 12;
// A picture's size on the page: a pixel is three quarters of a point (96 to 72 per inch).
const POINTS_PER_PIXEL = 0.75;
const TEXT_COLOR = '#000000';
const FOOTER_COLOR = '#666666';
const RULE_COLOR = '#d1d5db';
const TABLE_HEAD_FILL = '#f2f3f3';
const CODE_FILL = '#f4f4f4';
const QUOTE_COLOR = '#414d5c';

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
  if (isWritable(ch) || ch === '\n') return ch;
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

/**
 * Whether the standard fonts cannot write `text` as it is: it holds a
 * character outside Windows-1252 that is not a space or a zero-width
 * character. Such text is drawn as a picture when a rasterizer is given.
 */
export const needsRasterText = (text) => {
  const value = String(text ?? '');
  if (/^[\x20-\x7e\s]*$/.test(value)) return false;
  return Array.from(value.normalize('NFC')).some((ch) => !isWritable(ch) && !ZERO_WIDTH.test(ch) && !/\s/.test(ch));
};

/** Whether a table block has too many columns for a portrait page: seven or more. */
export const isWideTable = (block) =>
  Math.max((block?.header || []).length, ...(block?.rows || []).map((row) => row.length), 0) >= 7;

const runsNeedRaster = (runs) => (runs || []).some((run) => needsRasterText(run.text));

const pause = () =>
  new Promise((resolve) => {
    setTimeout(resolve, 0);
  });

const fontStyle = (bold, italic) => {
  if (bold && italic) return 'bolditalic';
  if (bold) return 'bold';
  if (italic) return 'italic';
  return 'normal';
};

/**
 * Lays out `blocks` on the pages of a new jsPDF document and returns it.
 *
 * Blocks:
 * - { kind: 'heading', level (0 to 3), runs, anchor?, color?, size? } -- kept
 *   with the line after it; `anchor` records its page in `anchors`.
 * - { kind: 'paragraph', runs, size?, after?, indent? }
 * - { kind: 'bullet', level, runs, marker? } -- `marker` is the text before it
 *   ('•' by default, '3.' for a numbered list), or { task: true, checked }.
 * - { kind: 'image', image: { data, width, height, format? }, id, maxHeight?,
 *   center?, landscapeIfWide? } -- width and height in pixels; JPEG unless
 *   `format` says PNG.
 * - { kind: 'table', header: [runs], rows: [[runs]], align?: ['left'|'center'|'right'] }
 * - { kind: 'code', text } -- monospace, whitespace kept, long lines wrapped.
 * - { kind: 'quote', runs } -- indented, with a bar.
 * - { kind: 'rule' }, { kind: 'spacer', height }
 * - { kind: 'pageBreak', orientation? } -- the next block starts a new page.
 * - { kind: 'toc', entries: [{ text, anchor }] } -- a contents list whose page
 *   numbers are written once the document is laid out, each a link to its page.
 *
 * A run is { text, bold, italic, mono, strike, color }, color as 'rrggbb' (as in docx).
 * `footer` is written at the foot of every page, left, with "Page n of m" at
 * the right. `orientation` is the pages' default.
 */
export const buildPdf = async (
  blocks,
  {
    jsPDF,
    title = '',
    compress = true,
    yieldEvery = 100,
    format = 'letter',
    orientation = 'portrait',
    footer = '',
    rasterizeText = null,
    anchors = {},
  } = {},
) => {
  // eslint-disable-next-line new-cap -- jsPDF is the library's own name for its constructor
  const doc = new jsPDF({ unit: 'pt', format, compress, orientation });
  if (title) doc.setProperties({ title: pdfSafeText(title) });
  let pageWidth = doc.internal.pageSize.getWidth();
  let pageHeight = doc.internal.pageSize.getHeight();
  let contentWidth = pageWidth - 2 * MARGIN;
  let bottom = pageHeight - MARGIN;
  let pageOrientation = orientation;
  let y = MARGIN;
  const tocEntries = [];

  // The font is set only when it changes: measuring thousands of words would
  // otherwise spend most of its time switching fonts.
  let current = '';
  const useFont = (bold, size, { italic = false, mono = false } = {}) => {
    const key = `${mono ? 'm' : 'h'}${fontStyle(bold, italic)}${size}`;
    if (key === current) return;
    doc.setFont(mono ? MONO_FONT : FONT, fontStyle(bold, italic));
    doc.setFontSize(size);
    current = key;
  };
  const measure = (text, bold, size, style) => {
    useFont(bold, size, style);
    return doc.getTextWidth(text);
  };

  const newPage = (wanted = orientation) => {
    doc.addPage(format, wanted);
    pageOrientation = wanted;
    pageWidth = doc.internal.pageSize.getWidth();
    pageHeight = doc.internal.pageSize.getHeight();
    contentWidth = pageWidth - 2 * MARGIN;
    bottom = pageHeight - MARGIN;
    y = MARGIN;
  };

  const room = (height) => {
    if (y + height > bottom && y > MARGIN) newPage(pageOrientation);
  };

  // Runs as lines no wider than `width`: each line a list of pieces
  // { text, bold, italic, mono, strike, color, width }. A word wider than the line is broken.
  const layoutLines = (runs, width, size, forceBold) => {
    const lines = [[]];
    let lineWidth = 0;
    const newLine = () => {
      const line = lines[lines.length - 1];
      while (line.length && !line[line.length - 1].text.trim()) lineWidth -= line.pop().width;
      lines.push([]);
      lineWidth = 0;
    };
    const place = (text, style) => {
      const w = measure(text, style.bold, style.size, style);
      lines[lines.length - 1].push({ ...style, text, width: w });
      lineWidth += w;
    };
    runs.forEach((run) => {
      const style = {
        bold: Boolean(forceBold || run.bold),
        italic: Boolean(run.italic),
        mono: Boolean(run.mono),
        strike: Boolean(run.strike),
        color: run.color ? `#${run.color}` : TEXT_COLOR,
        size: run.mono ? size * 0.92 : size,
      };
      const text = pdfSafeText(String(run.text ?? '').replace(/\r\n?/g, '\n'));
      text
        .split(/(\n|[ \t]+)/)
        .filter((token) => token !== '')
        .forEach((token) => {
          if (token === '\n') {
            newLine();
            return;
          }
          if (!token.trim()) {
            if (lines[lines.length - 1].length) place(' ', style);
            return;
          }
          let word = token;
          let w = measure(word, style.bold, style.size, style);
          if (lineWidth + w > width && lines[lines.length - 1].length) {
            newLine();
          }
          // A word wider than a whole line: as many characters as fit, line by line.
          while (w > width && word.length > 1) {
            let end = word.length - 1;
            while (end > 1 && measure(word.slice(0, end), style.bold, style.size, style) > width) end -= 1;
            place(word.slice(0, end), style);
            newLine();
            word = word.slice(end);
            w = measure(word, style.bold, style.size, style);
          }
          place(word, style);
        });
    });
    newLine();
    lines.pop();
    return lines.length ? lines : [[]];
  };

  // The lines of a block: text lines, or pictures of its lines when the
  // standard fonts cannot write it and a rasterizer was given.
  const blockLines = (runs, width, size, bold) => {
    if (rasterizeText && runsNeedRaster(runs)) {
      const pictures = rasterizeText(runs, { width, size, bold, lineHeight: size * LINE_HEIGHT });
      if (pictures?.length) return pictures.map((picture) => ({ picture }));
    }
    return layoutLines(runs, width, size, bold);
  };

  const drawLine = (line, x, size) => {
    if (line.picture) {
      const { data, width, height, rtl } = line.picture;
      doc.addImage(data, 'PNG', rtl ? x + line.columnWidth - width : x, y, width, height, undefined, 'FAST');
      return;
    }
    let cursor = x;
    // Neighbouring pieces in one style are written as one string, so the
    // text copies out of the PDF as it was written.
    const groups = [];
    line.forEach((piece) => {
      const last = groups[groups.length - 1];
      if (
        last &&
        last.bold === piece.bold &&
        last.color === piece.color &&
        last.italic === piece.italic &&
        last.mono === piece.mono &&
        last.strike === piece.strike
      ) {
        last.text += piece.text;
        last.width += piece.width;
      } else groups.push({ ...piece });
    });
    groups.forEach((group) => {
      const groupSize = group.size || size;
      useFont(group.bold, groupSize, group);
      doc.setTextColor(group.color);
      doc.text(group.text, cursor, y + (size - groupSize) / 2, { baseline: 'top' });
      if (group.strike) {
        doc.setDrawColor(group.color);
        doc.setLineWidth(0.6);
        doc.line(cursor, y + size * 0.55, cursor + group.width, y + size * 0.55);
      }
      cursor += group.width;
    });
  };

  const drawMarker = (marker, x, size) => {
    if (marker && typeof marker === 'object' && marker.task) {
      const box = size * 0.8;
      const top = y + size * 0.15;
      doc.setDrawColor('#5f6b7a');
      doc.setLineWidth(0.7);
      doc.rect(x, top, box, box);
      if (marker.checked) {
        doc.setLineWidth(1.1);
        doc.line(x + box * 0.2, top + box * 0.55, x + box * 0.42, top + box * 0.8);
        doc.line(x + box * 0.42, top + box * 0.8, x + box * 0.85, top + box * 0.2);
      }
      return;
    }
    useFont(false, size);
    doc.setTextColor(TEXT_COLOR);
    doc.text(pdfSafeText(marker || '•'), x, y, { baseline: 'top' });
  };

  const writeRuns = (runs, { x, width, size, bold = false, after = PARAGRAPH_AFTER, marker = null, onLine = null }) => {
    const lineHeight = size * LINE_HEIGHT;
    const lines = blockLines(runs, width, size, bold);
    lines.forEach((line, index) => {
      room(lineHeight);
      if (index === 0 && marker) drawMarker(marker.text, marker.x, size);
      if (onLine) onLine(lineHeight);
      // eslint-disable-next-line no-param-reassign -- where a right-to-left picture is aligned
      if (line.picture) line.columnWidth = width;
      drawLine(line, x, size);
      y += lineHeight;
    });
    y += after;
  };

  const writeImage = (block) => {
    const { data, width, height, format: imageFormat = 'JPEG' } = block.image;
    const natural = width * POINTS_PER_PIXEL;
    // A wide picture (a Gantt chart, say) that would be shrunk by more than a
    // quarter on a portrait page gets a landscape page of its own.
    if (
      block.landscapeIfWide &&
      pageOrientation === 'portrait' &&
      width > height * 1.3 &&
      natural > contentWidth * 1.3
    ) {
      newPage('landscape');
    }
    let w = Math.min(contentWidth, natural);
    let h = (height / width) * w;
    const maxHeight = Math.min(bottom - MARGIN, block.maxHeight || Infinity);
    if (h > maxHeight) {
      w *= maxHeight / h;
      h = maxHeight;
    }
    room(h);
    const x = block.center ? MARGIN + (contentWidth - w) / 2 : MARGIN;
    const alias = block.id ? `keyframe-${block.id}` : undefined;
    doc.addImage(data, imageFormat, x, y, w, h, alias, imageFormat === 'PNG' ? 'FAST' : 'NONE');
    y += h + IMAGE_AFTER;
  };

  // Column widths for a table: each column at least its longest word and at
  // most its longest cell, shared out so the table fills the text column.
  const columnWidths = (columns, cellsByColumn, size) => {
    const pad = 2 * CELL_PAD;
    const limit = contentWidth / 3;
    const mins = [];
    const maxes = [];
    cellsByColumn.slice(0, columns).forEach((cells) => {
      let min = 24;
      let max = 24;
      cells.forEach(({ runs, bold }) => {
        const text = runs.map((run) => pdfSafeText(run.text)).join('');
        text
          .split(/\s+/)
          .filter(Boolean)
          .forEach((word) => {
            min = Math.max(min, Math.min(measure(word, bold, size) + pad + 1, limit));
          });
        max = Math.max(max, measure(text, bold, size) + pad + 1);
      });
      mins.push(min);
      maxes.push(Math.max(min, max));
    });
    const sumMax = maxes.reduce((a, b) => a + b, 0);
    const sumMin = mins.reduce((a, b) => a + b, 0);
    if (sumMax <= contentWidth) {
      const spare = contentWidth - sumMax;
      return maxes.map((w) => w + (spare * w) / sumMax);
    }
    if (sumMin >= contentWidth) return mins.map((w) => (w * contentWidth) / sumMin);
    // A column whose longest cell fits in an even share keeps it whole; the
    // others share what is left, each at least its longest word.
    const share = contentWidth / columns;
    const narrow = maxes.map((w) => w <= share);
    const kept = maxes.reduce((sum, w, i) => sum + (narrow[i] ? w : 0), 0);
    const restMin = mins.reduce((sum, w, i) => sum + (narrow[i] ? 0 : w), 0);
    const restMax = maxes.reduce((sum, w, i) => sum + (narrow[i] ? 0 : w), 0);
    if (narrow.some(Boolean) && kept + restMin <= contentWidth && restMax > restMin) {
      const spare = contentWidth - kept - restMin;
      return maxes.map((w, i) => (narrow[i] ? w : mins[i] + (spare * (w - mins[i])) / (restMax - restMin)));
    }
    const flex = sumMax - sumMin;
    return mins.map((w, i) => w + ((contentWidth - sumMin) * (maxes[i] - w)) / flex);
  };

  const writeTable = (block) => {
    const header = block.header || [];
    const rows = block.rows || [];
    const columns = Math.max(header.length, ...rows.map((row) => row.length), 1);
    const cell = (row, c) => row[c] || [];
    const byColumn = Array.from({ length: columns }, (_, c) => [
      ...(header.length ? [{ runs: cell(header, c), bold: true }] : []),
      ...rows.map((row) => ({ runs: cell(row, c), bold: false })),
    ]);
    // A table too wide for a portrait page gets a landscape one.
    if (pageOrientation === 'portrait' && isWideTable(block)) newPage('landscape');
    const widths = columnWidths(columns, byColumn, TABLE_SIZE);
    const lineHeight = TABLE_SIZE * LINE_HEIGHT;
    const layoutRow = (row, bold) =>
      Array.from({ length: columns }, (_, c) => blockLines(cell(row, c), widths[c] - 2 * CELL_PAD, TABLE_SIZE, bold));
    const headerLines = header.length ? layoutRow(header, true) : null;
    const headerHeight = headerLines ? Math.max(...headerLines.map((l) => l.length)) * lineHeight + 2 * CELL_PAD : 0;

    const drawSlice = (cellLines, from, count, fill) => {
      const height = count * lineHeight + 2 * CELL_PAD;
      let x = MARGIN;
      doc.setDrawColor(RULE_COLOR);
      doc.setLineWidth(0.5);
      cellLines.forEach((lines, c) => {
        if (fill) {
          doc.setFillColor(fill);
          doc.rect(x, y, widths[c], height, 'FD');
        } else doc.rect(x, y, widths[c], height);
        const top = y;
        y += CELL_PAD;
        lines.slice(from, from + count).forEach((line) => {
          // eslint-disable-next-line no-param-reassign -- where a right-to-left picture is aligned
          if (line.picture) line.columnWidth = widths[c] - 2 * CELL_PAD;
          let lineX = x + CELL_PAD;
          const align = block.align?.[c];
          if (!line.picture && align && align !== 'left') {
            const used = line.reduce((sum, piece) => sum + piece.width, 0);
            const free = widths[c] - 2 * CELL_PAD - used;
            lineX += align === 'center' ? free / 2 : free;
          }
          drawLine(line, lineX, TABLE_SIZE);
          y += lineHeight;
        });
        y = top;
        x += widths[c];
      });
      y += height;
    };
    const drawHeader = () => {
      if (headerLines)
        drawSlice(
          headerLines,
          0,
          headerLines[0].length ? Math.max(...headerLines.map((l) => l.length)) : 1,
          TABLE_HEAD_FILL,
        );
    };

    room(headerHeight + lineHeight + 2 * CELL_PAD);
    drawHeader();
    rows.forEach((row) => {
      const cellLines = layoutRow(row, false);
      const total = Math.max(1, ...cellLines.map((lines) => lines.length));
      let done = 0;
      while (done < total) {
        let fits = Math.floor((bottom - y - 2 * CELL_PAD) / lineHeight);
        if (fits < Math.min(total - done, 2) && y > MARGIN + headerHeight) {
          newPage(pageOrientation);
          drawHeader();
          fits = Math.floor((bottom - y - 2 * CELL_PAD) / lineHeight);
        }
        const count = Math.max(1, Math.min(total - done, fits));
        drawSlice(cellLines, done, count, null);
        done += count;
      }
    });
    y += PARAGRAPH_AFTER + 4;
  };

  const writeCode = (block) => {
    const lineHeight = CODE_SIZE * 1.3;
    const width = contentWidth - 2 * CODE_PAD;
    const source = String(block.text ?? '')
      .replace(/\r\n?/g, '\n')
      .replace(/\t/g, '    ');
    const lines = [];
    source.split('\n').forEach((raw) => {
      // Spaces are kept: each source line is broken only where it is too long.
      let rest = raw;
      if (rasterizeText && needsRasterText(rest)) {
        const pictures = rasterizeText([{ text: rest || ' ', mono: true }], { width, size: CODE_SIZE, lineHeight });
        pictures.forEach((picture) => lines.push({ picture }));
        return;
      }
      rest = pdfSafeText(rest);
      if (!rest) {
        lines.push('');
        return;
      }
      while (rest.length) {
        let end = rest.length;
        while (end > 1 && measure(rest.slice(0, end), false, CODE_SIZE, { mono: true }) > width) end -= 1;
        lines.push(rest.slice(0, end));
        rest = rest.slice(end);
      }
    });
    let index = 0;
    // A block that fits on one page is kept together; a longer one starts
    // where it is and continues page by page.
    const whole = lines.length * lineHeight + 2 * CODE_PAD;
    room(whole <= bottom - MARGIN ? whole : lineHeight * 3 + 2 * CODE_PAD);
    while (index < lines.length) {
      room(lineHeight + 2 * CODE_PAD);
      const fits = Math.max(1, Math.floor((bottom - y - 2 * CODE_PAD) / lineHeight));
      const slice = lines.slice(index, index + fits);
      const height = slice.length * lineHeight + 2 * CODE_PAD;
      doc.setFillColor(CODE_FILL);
      doc.setDrawColor(RULE_COLOR);
      doc.setLineWidth(0.5);
      doc.rect(MARGIN, y, contentWidth, height, 'FD');
      y += CODE_PAD;
      // eslint-disable-next-line no-restricted-syntax -- a plain loop: each line moves y down
      for (const line of slice) {
        if (line && line.picture) {
          doc.addImage(
            line.picture.data,
            'PNG',
            MARGIN + CODE_PAD,
            y,
            line.picture.width,
            line.picture.height,
            undefined,
            'FAST',
          );
        } else if (line) {
          useFont(false, CODE_SIZE, { mono: true });
          doc.setTextColor('#16191f');
          doc.text(line, MARGIN + CODE_PAD, y, { baseline: 'top' });
        }
        y += lineHeight;
      }
      y += CODE_PAD;
      index += slice.length;
    }
    y += PARAGRAPH_AFTER + 2;
  };

  const writeToc = (block) => {
    (block.entries || []).forEach((entry) => {
      const size = BODY_SIZE + 0.5;
      const lineHeight = size * 1.6;
      room(lineHeight);
      useFont(false, size);
      doc.setTextColor(TEXT_COLOR);
      const text = pdfSafeText(entry.text);
      const available = contentWidth - 40;
      let shown = text;
      while (shown.length > 1 && doc.getTextWidth(shown) > available) shown = shown.slice(0, -1);
      if (shown !== text) shown = `${shown.slice(0, -1)}…`;
      const raster =
        rasterizeText && needsRasterText(entry.text)
          ? rasterizeText([{ text: entry.text }], { width: available, size, lineHeight: size * LINE_HEIGHT })
          : null;
      if (raster?.length) {
        const [picture] = raster;
        doc.addImage(picture.data, 'PNG', MARGIN, y, picture.width, picture.height, undefined, 'FAST');
      } else doc.text(shown, MARGIN, y, { baseline: 'top' });
      tocEntries.push({ page: doc.getCurrentPageInfo().pageNumber, y, anchor: entry.anchor, size, lineHeight });
      y += lineHeight;
    });
    y += PARAGRAPH_AFTER;
  };

  // The height a picture takes on the page, as writeImage places it.
  const imageHeight = (block) => {
    const { width, height } = block.image;
    const w = Math.min(contentWidth, width * POINTS_PER_PIXEL);
    return Math.min((height / width) * w, bottom - MARGIN, block.maxHeight || Infinity) + IMAGE_AFTER;
  };

  // What a heading is kept with: two lines of what follows it, or, when the
  // next block after its small notes is a picture, the picture and the notes.
  const keptWithHeading = (index) => {
    let notes = 0;
    for (let j = index + 1; j < blocks.length; j += 1) {
      const next = blocks[j];
      if (next.kind === 'paragraph' && next.size && next.size < BODY_SIZE) {
        notes += next.size * LINE_HEIGHT + (next.after ?? PARAGRAPH_AFTER);
      } else {
        return next.kind === 'image' && next.image ? notes + imageHeight(next) : BODY_SIZE * LINE_HEIGHT * 2;
      }
    }
    return BODY_SIZE * LINE_HEIGHT * 2;
  };

  const writeBlock = (block, index) => {
    if (block.kind === 'heading') {
      const size = block.size || HEADING_SIZES[block.level] || HEADING_SIZES[3];
      if (y > MARGIN) y += size * 0.5;
      // A heading is kept with what follows it, as far as a page allows.
      room(Math.min(size * LINE_HEIGHT + keptWithHeading(index), bottom - MARGIN));
      // eslint-disable-next-line no-param-reassign -- the caller reads each heading's page here
      if (block.anchor) anchors[block.anchor] = doc.getCurrentPageInfo().pageNumber;
      const runs = block.color ? block.runs.map((run) => ({ color: block.color, ...run })) : block.runs;
      writeRuns(runs, { x: MARGIN, width: contentWidth, size, bold: true, after: size * 0.4 });
    } else if (block.kind === 'bullet') {
      const indent = MARGIN + BULLET_INDENT * (block.level || 0);
      const markerText = block.marker || '•';
      const gap =
        typeof markerText === 'string' && markerText.length > 1
          ? BULLET_GAP + 6
          : BULLET_GAP + (markerText.task ? 4 : 0);
      writeRuns(block.runs, {
        x: indent + gap,
        width: contentWidth - (indent - MARGIN) - gap,
        size: BODY_SIZE,
        after: BULLET_AFTER,
        marker: { text: markerText, x: indent },
      });
    } else if (block.kind === 'image' && block.image) {
      writeImage(block);
    } else if (block.kind === 'table') {
      writeTable(block);
    } else if (block.kind === 'code') {
      writeCode(block);
    } else if (block.kind === 'quote') {
      const x = MARGIN + QUOTE_INDENT;
      const runs = (block.runs || []).map((run) => ({ color: QUOTE_COLOR.slice(1), ...run }));
      writeRuns(runs, {
        x,
        width: contentWidth - QUOTE_INDENT,
        size: BODY_SIZE,
        onLine: (lineHeight) => {
          doc.setFillColor(RULE_COLOR);
          doc.rect(MARGIN + 2, y, 2.5, lineHeight, 'F');
        },
      });
    } else if (block.kind === 'rule') {
      room(12);
      doc.setDrawColor(block.color || RULE_COLOR);
      doc.setLineWidth(block.weight || 0.75);
      doc.line(MARGIN, y + 4, MARGIN + (block.width || contentWidth), y + 4);
      y += 12;
    } else if (block.kind === 'spacer') {
      y = Math.min(bottom, y + (block.height || 12));
    } else if (block.kind === 'pageBreak') {
      const wanted = block.orientation || orientation;
      if (y > MARGIN || wanted !== pageOrientation) newPage(wanted);
    } else if (block.kind === 'toc') {
      writeToc(block);
    } else if (block.runs) {
      const size = block.size || BODY_SIZE;
      const x = MARGIN + (block.indent || 0);
      writeRuns(block.runs, {
        x,
        width: contentWidth - (block.indent || 0),
        size,
        after: block.after ?? PARAGRAPH_AFTER,
      });
    }
  };

  for (let i = 0; i < blocks.length; i += 1) {
    if (i > 0 && i % yieldEvery === 0) {
      // eslint-disable-next-line no-await-in-loop -- the pause is the point: it lets the page respond
      await pause();
    }
    writeBlock(blocks[i], i);
  }

  // The contents' page numbers, now that every page is known, each a link to its page.
  tocEntries.forEach((entry) => {
    const target = anchors[entry.anchor];
    if (!target) return;
    doc.setPage(entry.page);
    const width = doc.internal.pageSize.getWidth();
    useFont(false, entry.size);
    doc.setTextColor(FOOTER_COLOR);
    doc.text(String(target), width - MARGIN, entry.y, { baseline: 'top', align: 'right' });
    doc.link(MARGIN, entry.y, width - 2 * MARGIN, entry.lineHeight, { pageNumber: target });
  });

  // The footer text and "Page 2 of 7" at the foot of each page.
  const pages = doc.getNumberOfPages();
  const footerText = pdfSafeText(footer);
  for (let page = 1; page <= pages; page += 1) {
    doc.setPage(page);
    const width = doc.internal.pageSize.getWidth();
    const height = doc.internal.pageSize.getHeight();
    useFont(false, 8);
    doc.setTextColor(FOOTER_COLOR);
    if (footerText) {
      let shown = footerText;
      while (shown.length > 1 && doc.getTextWidth(shown) > width - 2 * MARGIN - 90) shown = shown.slice(0, -1);
      doc.text(shown === footerText ? shown : `${shown.slice(0, -1)}…`, MARGIN, height - MARGIN / 2);
    }
    doc.text(`Page ${page} of ${pages}`, width - MARGIN, height - MARGIN / 2, { align: 'right' });
  }
  return doc;
};
