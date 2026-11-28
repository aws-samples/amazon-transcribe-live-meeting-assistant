/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Text the PDF's standard fonts cannot write (another script, an emoji),
 * drawn as pictures with the browser's own fonts, for buildPdf's
 * `rasterizeText` (pdf-export.js). The text is wrapped to the column's width
 * -- between words, and between any two characters of a script written
 * without spaces (Chinese, Japanese, Thai) -- and each line is one PNG, as
 * tall as a line of text, so the page breaks fall between lines as they do
 * for text. Pictures are drawn at three times their size on the page, so
 * they print sharply.
 *
 * A line whose first strong character is right-to-left (Arabic, Hebrew) is
 * drawn right-to-left and set against the right edge of the column.
 */

const SCALE = 3;
const FONT_STACK =
  'system-ui, -apple-system, "Segoe UI", Roboto, "Noto Sans", "Noto Sans CJK SC", "Noto Sans JP", ' +
  '"Apple Color Emoji", "Segoe UI Emoji", sans-serif';
const MONO_STACK = 'ui-monospace, Menlo, Consolas, "Noto Sans Mono", monospace';

// One token: a character of a script written without spaces, a run of
// spaces, a line break, or a word.
const NO_SPACE_SCRIPTS =
  '\\p{Script=Han}\\p{Script=Hiragana}\\p{Script=Katakana}\\p{Script=Thai}\\p{Script=Lao}\\p{Script=Khmer}' +
  '\\p{Script=Myanmar}\\u3000-\\u303f\\uff00-\\uffef';
const TOKEN = new RegExp(`\\n|[ \\t]+|[${NO_SPACE_SCRIPTS}]|[^\\s${NO_SPACE_SCRIPTS}]+`, 'gu');
const RTL_FIRST = /^[^\p{L}]*[\p{Script=Arabic}\p{Script=Hebrew}\p{Script=Syriac}\p{Script=Thaana}]/u;

// Punctuation that may not start a line (a closing bracket, a full stop): it
// stays with the character before it.
const NO_LINE_START = /^[。、，．！？：；）」』】〉》〕｝・ーぁぃぅぇぉっゃゅょァィゥェォッャュョ々.,!?;:)\]}%]$/u;

/** The tokens of a text, in order (exported for tests). */
export const textTokens = (text) => {
  const tokens = [];
  (String(text ?? '').match(TOKEN) || []).forEach((token) => {
    const last = tokens[tokens.length - 1];
    if (last && last.trim() && NO_LINE_START.test(token)) tokens[tokens.length - 1] = last + token;
    else tokens.push(token);
  });
  return tokens;
};

/** Whether a line reads right to left: its first letter is in a right-to-left script. */
export const isRightToLeft = (text) => RTL_FIRST.test(String(text ?? ''));

const cssColor = (run) => (run.color ? `#${run.color}` : '#000000');

const fontFor = (run, size, bold) =>
  `${run.italic ? 'italic ' : ''}${bold || run.bold ? 'bold ' : ''}${size}px ${run.mono ? MONO_STACK : FONT_STACK}`;

/**
 * Wraps runs into lines no wider than `width` (in the units `measure` answers
 * in): [[{ text, run, width }]]. A token wider than a line is broken between
 * characters.
 */
export const wrapRuns = (runs, width, measure) => {
  const lines = [[]];
  let used = 0;
  const newLine = () => {
    const line = lines[lines.length - 1];
    while (line.length && !line[line.length - 1].text.trim()) used -= line.pop().width;
    lines.push([]);
    used = 0;
  };
  (runs || []).forEach((run) => {
    textTokens(String(run.text ?? '').replace(/\r\n?/g, '\n')).forEach((token) => {
      if (token === '\n') {
        newLine();
        return;
      }
      if (!token.trim()) {
        if (lines[lines.length - 1].length) {
          const w = measure(' ', run);
          lines[lines.length - 1].push({ text: ' ', run, width: w });
          used += w;
        }
        return;
      }
      let rest = token;
      let w = measure(rest, run);
      if (used + w > width && lines[lines.length - 1].length) newLine();
      while (w > width && Array.from(rest).length > 1) {
        const chars = Array.from(rest);
        let end = chars.length - 1;
        while (end > 1 && measure(chars.slice(0, end).join(''), run) > width) end -= 1;
        const head = chars.slice(0, end).join('');
        lines[lines.length - 1].push({ text: head, run, width: measure(head, run) });
        newLine();
        rest = chars.slice(end).join('');
        w = measure(rest, run);
      }
      lines[lines.length - 1].push({ text: rest, run, width: w });
      used += w;
    });
  });
  newLine();
  lines.pop();
  return lines.length ? lines : [[]];
};

const dataUrlBytes = (dataUrl) => {
  const binary = atob(dataUrl.slice(dataUrl.indexOf(',') + 1));
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
  return bytes;
};

/**
 * A rasterizer for buildPdf, drawing on canvases made in `doc`. Answers each
 * line of the runs as { data: PNG bytes, width, height, rtl } in points.
 */
export const createTextRasterizer =
  ({ doc = document, scale = SCALE } = {}) =>
  (runs, { width, size, bold = false, lineHeight = size * 1.35 }) => {
    const canvas = doc.createElement('canvas');
    const context = canvas.getContext('2d');
    if (!context) return null;
    const px = size * scale;
    const measure = (text, run) => {
      context.font = fontFor(run, px, bold);
      return context.measureText(text).width;
    };
    const lines = wrapRuns(runs, width * scale, measure);
    return lines.map((line) => {
      const text = line.map((piece) => piece.text).join('');
      const rtl = isRightToLeft(text);
      const used = line.reduce((sum, piece) => sum + piece.width, 0);
      const lineWidth = Math.max(1, Math.ceil(Math.min(width * scale, used + 2)));
      const lineHeightPx = Math.ceil(lineHeight * scale);
      const picture = doc.createElement('canvas');
      picture.width = lineWidth;
      picture.height = lineHeightPx;
      const ctx = picture.getContext('2d');
      ctx.textBaseline = 'middle';
      const middle = lineHeightPx / 2;
      if (rtl) {
        // The line as one string, so the browser orders it; one style for the line.
        ctx.direction = 'rtl';
        ctx.textAlign = 'right';
        const run = line[0]?.run || {};
        ctx.font = fontFor(run, px, bold);
        ctx.fillStyle = cssColor(run);
        ctx.fillText(text, lineWidth, middle);
      } else {
        let x = 0;
        line.forEach((piece) => {
          ctx.font = fontFor(piece.run, px, bold);
          ctx.fillStyle = cssColor(piece.run);
          ctx.fillText(piece.text, x, middle);
          x += piece.width;
        });
      }
      return {
        data: dataUrlBytes(picture.toDataURL('image/png')),
        width: lineWidth / scale,
        height: lineHeightPx / scale,
        rtl,
      };
    });
  };

/**
 * The rasterizer the downloads use in the browser, or null where there is no
 * canvas to draw on (a test's document), in which case buildPdf writes such
 * text as pdfSafeText does.
 */
export const browserTextRasterizer = () => {
  if (typeof document === 'undefined' || typeof navigator === 'undefined' || /jsdom/i.test(navigator.userAgent)) {
    return null;
  }
  return createTextRasterizer();
};
