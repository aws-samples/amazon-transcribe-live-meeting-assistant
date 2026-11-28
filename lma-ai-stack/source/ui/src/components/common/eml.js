/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * An email draft as a `.eml` file (an Internet Message Format message, RFC
 * 5322, in MIME), for Email participants (MeetingPackModal.jsx, mode 'email'):
 *
 *   multipart/mixed
 *     multipart/alternative
 *       text/plain; charset=UTF-8   (the summary as text)
 *       text/html; charset=UTF-8    (the summary as a web page)
 *     application/pdf ...           (each file chosen, as an attachment)
 *
 * The message has no From: (the mail app fills in the account that sends it)
 * and carries `X-Unsent: 1`, which Outlook reads as "open this as a draft to
 * edit and send" rather than as a message received. Names and the subject are
 * written as RFC 2047 encoded words when they hold anything but plain ASCII,
 * an attachment's name also in the RFC 2231 form, every body part in base64
 * with lines of 76 characters, and every line ends in CRLF. Long header lines
 * are folded, so a meeting with a hundred participants is still a valid
 * message. Nothing here is sent anywhere; the file is saved to the computer.
 */

const CRLF = '\r\n';
const LINE = 76;

/** Bytes as base64, in chunks so a large file does not overflow the call stack. */
export const bytesToBase64 = (bytes) => {
  let binary = '';
  const CHUNK = 0x8000;
  for (let i = 0; i < bytes.length; i += CHUNK) binary += String.fromCharCode(...bytes.subarray(i, i + CHUNK));
  return btoa(binary);
};

/** Base64 as lines of at most 76 characters, each ended by CRLF. */
export const base64Lines = (bytes) => {
  const text = bytesToBase64(bytes);
  const lines = [];
  for (let i = 0; i < text.length; i += LINE) lines.push(text.slice(i, i + LINE));
  return lines.join(CRLF);
};

const utf8 = (text) => new TextEncoder().encode(String(text ?? ''));

// Printable ASCII with no character that would need quoting or encoding in a header.
const isPlainAscii = (text) => /^[\x20-\x7e]*$/.test(text) && !/=\?/.test(text);

/**
 * Text as RFC 2047 encoded words ("=?UTF-8?B?...?="), each at most 75
 * characters, split between characters (never inside one), or the text
 * itself when it is plain ASCII. Several words are joined by a space, which
 * a reader removes between encoded words.
 */
export const encodeWords = (text, { force = false, bytes = 39 } = {}) => {
  const value = String(text ?? '');
  if (!force && isPlainAscii(value)) return [value];
  const words = [];
  let chunk = '';
  // 39 bytes are 52 base64 characters: "=?UTF-8?B?" + 52 + "?=" is 64, which fits
  // on a line after "Subject: ". A word
  // inside a parameter is given fewer, to leave room for the parameter's name.
  Array.from(value).forEach((ch) => {
    if (utf8(chunk + ch).length > bytes) {
      words.push(chunk);
      chunk = '';
    }
    chunk += ch;
  });
  if (chunk || !words.length) words.push(chunk);
  return words.map((word) => `=?UTF-8?B?${bytesToBase64(utf8(word))}?=`);
};

/** A quoted string, for a display name or a parameter: "..." with \ and " escaped. */
const quoted = (text) => `"${String(text).replace(/[\\"]/g, '\\$&')}"`;

/** One address as a header writes it: `"Name" <email>`, the name encoded when it is not ASCII. */
export const formatAddress = ({ name = '', email }) => {
  const display = String(name || '')
    .replace(/[\r\n]+/g, ' ')
    .trim();
  if (!display) return email;
  if (isPlainAscii(display)) return `${quoted(display)} <${email}>`;
  return `${encodeWords(display).join(' ')} <${email}>`;
};

/**
 * A header line, folded: `Name: ` then the pieces, each kept whole on a line
 * and joined by `separator`, a new line (CRLF and a space) started before a
 * piece that would take the line past 76 characters.
 */
export const foldHeader = (name, pieces, separator = ' ') => {
  const lines = [];
  let line = `${name}:`;
  pieces.forEach((piece, index) => {
    const text = index < pieces.length - 1 ? `${piece}${separator.trimEnd()}` : piece;
    if (line.length + 1 + text.length > LINE && line.trim() !== `${name}:`) {
      lines.push(line);
      line = ` ${text}`;
    } else line = `${line} ${text}`;
  });
  lines.push(line);
  return lines.join(CRLF);
};

/**
 * The addresses as the pieces a header line is folded between: each address
 * whole, except that a name written as several encoded words may be folded
 * between them; a comma after each address but the last.
 */
export const addressPieces = (to) =>
  to.flatMap((address, index) => {
    const text = formatAddress(address);
    const pieces = text.startsWith('=?') ? text.split(' ') : [text];
    if (index < to.length - 1) pieces[pieces.length - 1] += ',';
    return pieces;
  });

const DAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const pad = (n) => String(n).padStart(2, '0');

/** A date as RFC 5322 writes it, in UTC: "Sat, 28 Nov 2026 17:05:09 +0000". */
export const rfc5322Date = (date) =>
  `${DAYS[date.getUTCDay()]}, ${pad(date.getUTCDate())} ${MONTHS[date.getUTCMonth()]} ${date.getUTCFullYear()} ` +
  `${pad(date.getUTCHours())}:${pad(date.getUTCMinutes())}:${pad(date.getUTCSeconds())} +0000`;

const randomToken = () => {
  const bytes = new Uint8Array(12);
  (globalThis.crypto || window.crypto).getRandomValues(bytes);
  return [...bytes].map((b) => b.toString(16).padStart(2, '0')).join('');
};

const ATTRIBUTE_CHAR = /[A-Za-z0-9!#$&+.^_`|~-]/;
// The most percent-encoded characters one RFC 2231 section holds, so each
// section stays on a line of its own under 78 characters.
const SECTION_CHARS = 54;

/**
 * RFC 2231: `filename*=UTF-8''<percent-encoded bytes>`, or, when that is
 * long, continued as `filename*0*=UTF-8''...`, `filename*1*=...`, each
 * section cut between encoded characters.
 */
export const extendedParameters = (name, value) => {
  const encoded = [...utf8(value)].map((b) =>
    ATTRIBUTE_CHAR.test(String.fromCharCode(b)) && b < 0x80
      ? String.fromCharCode(b)
      : `%${b.toString(16).toUpperCase().padStart(2, '0')}`,
  );
  const whole = encoded.join('');
  if (whole.length <= SECTION_CHARS) return [`${name}*=UTF-8''${whole}`];
  const sections = [''];
  encoded.forEach((piece) => {
    if (sections[sections.length - 1].length + piece.length > SECTION_CHARS) sections.push('');
    sections[sections.length - 1] += piece;
  });
  return sections.map((section, n) => `${name}*${n}*=${n === 0 ? "UTF-8''" : ''}${section}`);
};

const fileParameters = (parameter, filename) => {
  const clean = String(filename).replace(/[\r\n"\\]/g, '_');
  if (isPlainAscii(clean)) return [`${parameter}=${quoted(clean)}`];
  // Both forms: RFC 2231 for the readers that know it, and the encoded words
  // that Outlook and older readers show, one per line.
  const words = encodeWords(clean, { bytes: 36 }).join(`${CRLF} `);
  return [`${parameter}="${words}"`, ...extendedParameters(parameter, clean)];
};

const part = (headers, body) => `${headers.join(CRLF)}${CRLF}${CRLF}${body}`;

/**
 * The message as a string. `to` are [{ name, email }]; `text` and `html` the
 * two forms of the body; `attachments` are [{ filename, contentType, data:
 * Uint8Array }]. `boundary` and `date` are for tests.
 */
export const buildEml = ({
  to = [],
  subject = '',
  text = '',
  html = '',
  attachments = [],
  date = new Date(),
  boundary = randomToken(),
}) => {
  const oneLine = String(subject ?? '').replace(/[\r\n]+/g, ' ');
  const mixed = `=_lma_mixed_${boundary}`;
  const alternative = `=_lma_alt_${boundary}`;
  const headers = [
    'X-Unsent: 1',
    to.length ? foldHeader('To', addressPieces(to)) : 'To: ',
    foldHeader('Subject', isPlainAscii(oneLine) ? oneLine.split(' ') : encodeWords(oneLine)),
    `Date: ${rfc5322Date(date)}`,
    'MIME-Version: 1.0',
    'X-Mailer: Live Meeting Assistant',
    `Content-Type: multipart/mixed;${CRLF} boundary="${mixed}"`,
  ];
  const bodies = part(
    [`Content-Type: multipart/alternative;${CRLF} boundary="${alternative}"`],
    [
      `--${alternative}`,
      part(
        ['Content-Type: text/plain; charset=UTF-8', 'Content-Transfer-Encoding: base64'],
        base64Lines(utf8(String(text).replace(/\r?\n/g, CRLF))),
      ),
      `--${alternative}`,
      part(['Content-Type: text/html; charset=UTF-8', 'Content-Transfer-Encoding: base64'], base64Lines(utf8(html))),
      `--${alternative}--`,
      '',
    ].join(CRLF),
  );
  const files = attachments.map(({ filename, contentType = 'application/octet-stream', data }) =>
    part(
      [
        `Content-Type: ${contentType};${CRLF} ${fileParameters('name', filename).join(`;${CRLF} `)}`,
        `Content-Disposition: attachment;${CRLF} ${fileParameters('filename', filename).join(`;${CRLF} `)}`,
        'Content-Transfer-Encoding: base64',
      ],
      base64Lines(data),
    ),
  );
  const body = [
    'This is a multi-part message in MIME format.',
    `--${mixed}`,
    bodies,
    ...files.flatMap((file) => [`--${mixed}`, file]),
    `--${mixed}--`,
    '',
  ].join(CRLF);
  return `${headers.join(CRLF)}${CRLF}${CRLF}${body}`;
};

/** The size, in bytes, a message with attachments of `bytes` comes to once encoded: base64 and line ends. */
export const encodedSize = (bytes) => Math.ceil(bytes / 3) * 4 * (78 / 76);

/** The draft as a file to save. */
export const emlBlob = (message) => new Blob([message], { type: 'message/rfc822' });
