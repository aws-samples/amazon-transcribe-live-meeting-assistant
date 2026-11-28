/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The email draft's message format: headers, the MIME tree, encoded names
 * and subject, folded lines, base64 bodies and attachments.
 */
import { describe, it, expect } from 'vitest';

import { buildEml, encodeWords, encodedSize, foldHeader, formatAddress, rfc5322Date } from './eml';

const decodeWord = (word) => {
  const match = word.match(/^=\?UTF-8\?B\?(.*)\?=$/);
  const bytes = Uint8Array.from(atob(match[1]), (c) => c.charCodeAt(0));
  return new TextDecoder().decode(bytes);
};
const decodeBase64 = (text) => {
  const bytes = Uint8Array.from(atob(text.replace(/\r\n/g, '')), (c) => c.charCodeAt(0));
  return bytes;
};

// The message split at its header block, its headers unfolded.
const parse = (message) => {
  const [head, ...rest] = message.split('\r\n\r\n');
  const headers = {};
  head
    .replace(/\r\n[ \t]/g, ' ')
    .split('\r\n')
    .forEach((line) => {
      const at = line.indexOf(':');
      headers[line.slice(0, at)] = line.slice(at + 1).trim();
    });
  return { headers, body: rest.join('\r\n\r\n') };
};

const PDF = new TextEncoder().encode('%PDF-1.3 a small file');
const MESSAGE = buildEml({
  to: [
    { name: 'Ana Lima', email: 'ana@example.com' },
    { name: '山田 太郎', email: 'yamada@example.jp' },
    { name: '', email: 'bo@example.org' },
  ],
  subject: 'Planning — 計画 summary',
  text: 'SUMMARY\n\nAgreed.',
  html: '<p>Agreed — ok</p>',
  attachments: [
    { filename: 'Summary-Planning.pdf', contentType: 'application/pdf', data: PDF },
    { filename: 'Whiteboards-計画.pdf', contentType: 'application/pdf', data: PDF },
  ],
  date: new Date('2026-11-28T17:05:09Z'),
  boundary: 'b1',
});

describe('buildEml', () => {
  it('is a draft Outlook opens to edit: X-Unsent, no From, MIME 1.0, a date', () => {
    const { headers } = parse(MESSAGE);
    expect(headers['X-Unsent']).toBe('1');
    expect(headers.From).toBeUndefined();
    expect(headers['MIME-Version']).toBe('1.0');
    expect(headers.Date).toBe('Sat, 28 Nov 2026 17:05:09 +0000');
  });

  it('holds the text and the HTML as alternatives inside multipart/mixed, then each attachment', () => {
    const { headers, body } = parse(MESSAGE);
    expect(headers['Content-Type']).toBe('multipart/mixed; boundary="=_lma_mixed_b1"');
    const parts = body.split('--=_lma_mixed_b1');
    expect(parts.at(-1).startsWith('--')).toBe(true);
    const [alternative, first, second] = parts.slice(1, -1);
    expect(alternative).toContain('Content-Type: multipart/alternative;\r\n boundary="=_lma_alt_b1"');
    const bodies = alternative.split('--=_lma_alt_b1');
    expect(bodies[1]).toContain('Content-Type: text/plain; charset=UTF-8');
    expect(bodies[2]).toContain('Content-Type: text/html; charset=UTF-8');
    expect(bodies[3].startsWith('--')).toBe(true);
    const text = new TextDecoder().decode(decodeBase64(bodies[1].split('\r\n\r\n')[1].trim()));
    expect(text).toBe('SUMMARY\r\n\r\nAgreed.');
    const html = new TextDecoder().decode(decodeBase64(bodies[2].split('\r\n\r\n')[1].trim()));
    expect(html).toBe('<p>Agreed — ok</p>');
    expect(first).toContain('Content-Disposition: attachment;\r\n filename="Summary-Planning.pdf"');
    expect(first).toContain('Content-Transfer-Encoding: base64');
    expect([...decodeBase64(first.split('\r\n\r\n')[1].trim())]).toEqual([...PDF]);
    expect(second).toContain("filename*=UTF-8''Whiteboards-%E8%A8%88%E7%94%BB.pdf");
  });

  it('encodes names and the subject that are not ASCII, and keeps plain ones readable', () => {
    const { headers } = parse(MESSAGE);
    const [ana, yamada, bo] = headers.To.split(', ');
    expect(ana).toBe('"Ana Lima" <ana@example.com>');
    expect(decodeWord(yamada.split(' <')[0])).toBe('山田 太郎');
    expect(bo).toBe('bo@example.org');
    expect(headers.Subject.split(' ').map(decodeWord).join('')).toBe('Planning — 計画 summary');
  });

  it('ends every line in CRLF, and no line is longer than 78 characters', () => {
    expect(MESSAGE.replace(/\r\n/g, '')).not.toMatch(/[\r\n]/);
    MESSAGE.split('\r\n').forEach((line) => expect(line.length).toBeLessThanOrEqual(78));
  });

  it('folds a long participant list onto lines that each stay short', () => {
    const to = Array.from({ length: 100 }, (_, i) => ({ name: `Person ${i}`, email: `p${i}@example.com` }));
    const message = buildEml({ to, subject: 'x', boundary: 'b' });
    const head = message.split('\r\n\r\n')[0];
    head.split('\r\n').forEach((line) => expect(line.length).toBeLessThanOrEqual(78));
    expect(parse(message).headers.To.split(', ')).toHaveLength(100);
  });

  it('leaves To: empty when there is no recipient, and writes a subject with a line break on one line', () => {
    const { headers } = parse(buildEml({ subject: 'One\r\ntwo', boundary: 'b' }));
    expect(headers.To).toBe('');
    expect(headers.Subject).toBe('One two');
  });
});

describe('long names', () => {
  const name = `Summary-${'会議の議事録'.repeat(25)}.pdf`;
  const message = buildEml({
    to: [{ name: '山田 太郎'.repeat(10), email: 'yamada@example.jp' }],
    subject: `${'長い会議名'.repeat(30)} summary`,
    attachments: [{ filename: name, contentType: 'application/pdf', data: PDF }],
    boundary: 'b',
  });

  it('keeps every line short, with a long name, subject and attachment name in another script', () => {
    message.split('\r\n').forEach((line) => expect(line.length).toBeLessThanOrEqual(78));
  });

  it("continues a long attachment name's RFC 2231 form in numbered sections that make up the name", () => {
    const sections = [...message.matchAll(/filename\*(\d+)\*=(?:UTF-8'')?([^;\r\n]+)/g)];
    expect(sections.length).toBeGreaterThan(2);
    expect(sections.map((match) => Number(match[1]))).toEqual(sections.map((_, n) => n));
    expect(decodeURIComponent(sections.map((match) => match[2]).join(''))).toBe(name);
    expect(message).not.toMatch(/filename\*=/);
  });
});

describe('header helpers', () => {
  it('quotes a display name holding commas or quotes', () => {
    expect(formatAddress({ name: 'Lima, "Ana"', email: 'a@example.com' })).toBe('"Lima, \\"Ana\\"" <a@example.com>');
  });

  it('splits a long name into encoded words of at most 75 characters, never inside a character', () => {
    const words = encodeWords('会議'.repeat(40));
    expect(words.length).toBeGreaterThan(1);
    words.forEach((word) => expect(word.length).toBeLessThanOrEqual(75));
    expect(words.map(decodeWord).join('')).toBe('会議'.repeat(40));
  });

  it('folds before a piece that would pass 76 characters', () => {
    const folded = foldHeader('To', ['a'.repeat(40), 'b'.repeat(40)], ', ');
    expect(folded).toBe(`To: ${'a'.repeat(40)},\r\n ${'b'.repeat(40)}`);
  });

  it('writes dates in UTC', () => {
    expect(rfc5322Date(new Date('2026-01-02T03:04:05Z'))).toBe('Fri, 02 Jan 2026 03:04:05 +0000');
  });

  it('counts the size an attachment comes to once encoded: a third more, and line ends', () => {
    expect(encodedSize(3 * 1024 * 1024)).toBeCloseTo(4 * 1024 * 1024 * (78 / 76), -2);
  });
});
