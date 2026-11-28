/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * An email draft as a `.eml` file (an Internet Message Format message, RFC
 * 5322, in MIME), for Email participants (EmailParticipantsModal.jsx):
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
export const encodeWords = (text, { force = false } = {}) => {
  const value = String(text ?? '');
  if (!force && isPlainAscii(value)) return [value];
  const words = [];
  let chunk = '';
  // 45 bytes are 60 base64 characters: "=?UTF-8?B?" + 60 + "?=" is 72.
  Array.from(value).forEach((ch) => {
    if (utf8(chunk + ch).length > 45) {
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
// RFC 2231: filename*=UTF-8''<percent-encoded bytes>.
const extendedParameter = (name, value) =>
  `${name}*=UTF-8''${[...utf8(value)]
    .map((b) =>
      ATTRIBUTE_CHAR.test(String.fromCharCode(b)) && b < 0x80
        ? String.fromCharCode(b)
        : `%${b.toString(16).toUpperCase().padStart(2, '0')}`,
    )
    .join('')}`;

const fileParameters = (parameter, filename) => {
  const clean = String(filename).replace(/[\r\n"\\]/g, '_');
  if (isPlainAscii(clean)) return [`${parameter}=${quoted(clean)}`];
  // Both forms: RFC 2231 for the readers that know it, and the encoded word
  // that Outlook and older readers show.
  return [`${parameter}="${encodeWords(clean).join(' ')}"`, extendedParameter(parameter, clean)];
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
    to.length ? foldHeader('To', to.map(formatAddress), ', ') : 'To: ',
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
