/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { extractChatLinks, findChatLinks, MAX_CHAT_LINK_LENGTH, splitChatText } from './chat-links';

// The table lma_chat_links' tests read too, so the two apply the same rules.
const FIXTURE = JSON.parse(
  readFileSync(path.join(__dirname, '../../../../../../test-fixtures/chat-links.json'), 'utf-8'),
);
// LONG<n> stands for <n> repetitions of the letter a (see the fixture's notes).
const expand = (text) => text.replace(/LONG(\d+)/g, (_, n) => 'a'.repeat(Number(n)));

// The scheme a chat message might hold, spelled out of a literal the linter reads as code.
const SCRIPT = ['java', 'script:'].join('');

describe('extractChatLinks, against the table shared with lma_chat_links', () => {
  it('has cases to check', () => {
    expect(FIXTURE.cases.length).toBeGreaterThan(20);
  });

  it.each(FIXTURE.cases.map((c) => [c.name, c]))('%s', (_, c) => {
    expect(extractChatLinks(expand(c.input))).toEqual(c.urls.map(expand));
  });

  it('puts the length cases either side of the limit', () => {
    expect(expand('https://example.com/LONG2028')).toHaveLength(MAX_CHAT_LINK_LENGTH);
    expect(expand('https://example.com/LONG2040').length).toBeGreaterThan(MAX_CHAT_LINK_LENGTH);
  });

  it('finds nothing in anything but text', () => {
    [undefined, null, 42, {}].forEach((value) => expect(extractChatLinks(value)).toEqual([]));
  });
});

describe('the rules, where JavaScript and Python differ by default', () => {
  it("ends a candidate at every character Python's \\s matches, and only those", () => {
    ['\u001c', '\u001f', '\u0085', '\u00a0', '\u2028', '\u3000'].forEach((space) =>
      expect(extractChatLinks(`https://example.com/a${space}b`)).toEqual(['https://example.com/a']),
    );
    // U+FEFF is whitespace to JavaScript's \s but not to Python's.
    expect(extractChatLinks('https://example.com/a\ufeffb')).toEqual(['https://example.com/a\ufeffb']);
  });

  it('starts a candidate after a letter outside ASCII, as Python does', () => {
    expect(extractChatLinks('\u00e9https://example.com')).toEqual(['https://example.com']);
  });

  it("takes any Unicode digit in a port, as Python's \\d does", () => {
    expect(extractChatLinks('http://localhost:\u0663\u0660')).toEqual(['http://localhost:\u0663\u0660']);
  });

  it('counts the length of the address, not of the text typed', () => {
    const typed = `www.example.com/${'a'.repeat(MAX_CHAT_LINK_LENGTH - 'https://www.example.com/'.length)}`;
    expect(extractChatLinks(typed)).toHaveLength(1);
    expect(extractChatLinks(`${typed}a`)).toEqual([]);
  });
});

describe('findChatLinks', () => {
  it('gives each address where it sits, as typed, and the address it stands for', () => {
    const text = 'Deck: www.Example.com/deck and HTTPS://example.com/a`b, again www.Example.com/deck';
    const links = findChatLinks(text);
    expect(links.map(({ url, address }) => [url, address])).toEqual([
      ['www.Example.com/deck', 'https://www.Example.com/deck'],
      ['HTTPS://example.com/a`b', 'https://example.com/a%60b'],
      ['www.Example.com/deck', 'https://www.Example.com/deck'],
    ]);
    links.forEach(({ start, end, url }) => expect(text.slice(start, end)).toBe(url));
  });

  it('finds nothing but http and https addresses, and only the http one inside another scheme', () => {
    expect(findChatLinks(`${SCRIPT}alert(1) data:text/html;base64,PHNjcmlwdD4=`)).toEqual([]);
    expect(findChatLinks(`${SCRIPT}window.open('https://example.com/x')`).map((l) => l.address)).toEqual([
      'https://example.com/x',
    ]);
  });

  it('copes with very long text', () => {
    expect(findChatLinks(`${'x'.repeat(100000)} https://example.com`).map((l) => l.url)).toEqual([
      'https://example.com',
    ]);
  });
});

describe('splitChatText', () => {
  it('cuts the text into its plain runs and its addresses, and joins back to the text', () => {
    const text = 'Deck (https://example.com/deck). Thanks!';
    const pieces = splitChatText(text);
    expect(pieces).toEqual([
      { kind: 'text', text: 'Deck (', start: 0 },
      { kind: 'link', text: 'https://example.com/deck', start: 6, address: 'https://example.com/deck' },
      { kind: 'text', text: '). Thanks!', start: 30 },
    ]);
    expect(pieces.map((p) => p.text).join('')).toBe(text);
  });

  it('is one plain run with no address, and nothing for no text', () => {
    expect(splitChatText('+1')).toEqual([{ kind: 'text', text: '+1', start: 0 }]);
    expect(splitChatText('')).toEqual([]);
  });

  it('is only the address when that is all the message holds', () => {
    expect(splitChatText('www.example.com')).toEqual([
      { kind: 'link', text: 'www.example.com', start: 0, address: 'https://www.example.com' },
    ]);
  });
});
