/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import { describe, it, expect } from 'vitest';
import { findChatLinks, MAX_CHAT_LINK_LENGTH, splitChatText } from './chat-links';

// The scheme a chat message might hold, spelled out of a literal the linter reads as code.
const SCRIPT = ['java', 'script:'].join('');

const urls = (text) => findChatLinks(text).map((link) => link.url);

describe('findChatLinks', () => {
  it('finds http, https and www. addresses, as typed, with where each sits', () => {
    const text = 'Deck: https://example.com/deck and notes at http://intranet.example/notes or www.example.org';
    const links = findChatLinks(text);
    expect(links.map((link) => link.url)).toEqual([
      'https://example.com/deck',
      'http://intranet.example/notes',
      'www.example.org',
    ]);
    links.forEach(({ start, end, url }) => expect(text.slice(start, end)).toBe(url));
    expect(links[0].start).toBe(6);
    expect(urls('HTTPS://EXAMPLE.COM/A')).toEqual(['HTTPS://EXAMPLE.COM/A']);
  });

  it('keeps the query and fragment', () => {
    expect(urls('see https://example.com/a?b=1&c=two#part')).toEqual(['https://example.com/a?b=1&c=two#part']);
  });

  it('leaves out the punctuation that ends the sentence', () => {
    expect(urls('Read https://example.com/doc.')).toEqual(['https://example.com/doc']);
    expect(urls('https://example.com/a, https://example.com/b; https://example.com/c!')).toEqual([
      'https://example.com/a',
      'https://example.com/b',
      'https://example.com/c',
    ]);
    expect(urls('Is it https://example.com/doc?')).toEqual(['https://example.com/doc']);
    expect(urls('https://example.com/doc...')).toEqual(['https://example.com/doc']);
    expect(urls('at https://example.com/doc: the second page')).toEqual(['https://example.com/doc']);
  });

  it('leaves out a closing bracket with no opening one in the address', () => {
    expect(urls('(see https://example.com/doc)')).toEqual(['https://example.com/doc']);
    expect(urls('(see https://example.com/doc).')).toEqual(['https://example.com/doc']);
    expect(urls('[https://example.com/doc]')).toEqual(['https://example.com/doc']);
    expect(urls('{https://example.com/doc}')).toEqual(['https://example.com/doc']);
  });

  it('keeps brackets that open and close inside the address', () => {
    expect(urls('https://en.wikipedia.org/wiki/Bracket_(disambiguation)')).toEqual([
      'https://en.wikipedia.org/wiki/Bracket_(disambiguation)',
    ]);
    expect(urls('(https://en.wikipedia.org/wiki/Bracket_(disambiguation))')).toEqual([
      'https://en.wikipedia.org/wiki/Bracket_(disambiguation)',
    ]);
  });

  it('stops at quotes and leaves out a closing quote', () => {
    expect(urls('"https://example.com/doc"')).toEqual(['https://example.com/doc']);
    expect(urls("'https://example.com/doc'")).toEqual(['https://example.com/doc']);
    expect(urls('“https://example.com/doc”')).toEqual(['https://example.com/doc']);
    expect(urls('href="https://example.com/a">x</a>')).toEqual(['https://example.com/a']);
  });

  it('stops at a backtick', () => {
    expect(urls('`https://example.com/doc`')).toEqual(['https://example.com/doc']);
    expect(urls('https://example.com/a`b')).toEqual(['https://example.com/a']);
    expect(urls('```https://example.com/x```')).toEqual(['https://example.com/x']);
  });

  it('finds nothing but http and https addresses', () => {
    expect(urls(`${SCRIPT}alert(1)`)).toEqual([]);
    expect(urls('data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==')).toEqual([]);
    expect(urls('ftp://example.com/file mailto:someone@example.com file:///etc/passwd')).toEqual([]);
    expect(urls('vbscript:msgbox')).toEqual([]);
  });

  it('finds only the http address inside another scheme, never the scheme itself', () => {
    expect(urls(`${SCRIPT}window.open('https://example.com/x')`)).toEqual(['https://example.com/x']);
  });

  it('does not take an address out of the middle of a word or an email address', () => {
    expect(urls('xhttps://example.com')).toEqual([]);
    expect(urls('someone@www.example.com')).toEqual([]);
    expect(urls('mirror.www.example.com')).toEqual([]);
  });

  it('needs a host', () => {
    expect(urls('https:// and www. and https://.')).toEqual([]);
    expect(urls('http://')).toEqual([]);
  });

  it(`leaves an address longer than ${MAX_CHAT_LINK_LENGTH} characters as text`, () => {
    const base = 'https://example.com/';
    const longest = base + 'a'.repeat(MAX_CHAT_LINK_LENGTH - base.length);
    expect(urls(longest)).toEqual([longest]);
    expect(urls(`${longest}a`)).toEqual([]);
    expect(urls(`${'x'.repeat(100000)} https://example.com`)).toEqual(['https://example.com']);
  });

  it('finds nothing in no text', () => {
    expect(findChatLinks('')).toEqual([]);
    expect(findChatLinks(undefined)).toEqual([]);
    expect(findChatLinks(null)).toEqual([]);
    expect(findChatLinks(42)).toEqual([]);
  });

  it('stops at a line break', () => {
    expect(urls('https://example.com/a\nhttps://example.com/b')).toEqual([
      'https://example.com/a',
      'https://example.com/b',
    ]);
  });
});

describe('splitChatText', () => {
  it('cuts the text into its plain runs and its addresses, and joins back to the text', () => {
    const text = 'Deck (https://example.com/deck). Thanks!';
    const pieces = splitChatText(text);
    expect(pieces).toEqual([
      { kind: 'text', text: 'Deck (', start: 0 },
      { kind: 'link', text: 'https://example.com/deck', start: 6 },
      { kind: 'text', text: '). Thanks!', start: 30 },
    ]);
    expect(pieces.map((p) => p.text).join('')).toBe(text);
  });

  it('is one plain run with no address, and nothing for no text', () => {
    expect(splitChatText('+1')).toEqual([{ kind: 'text', text: '+1', start: 0 }]);
    expect(splitChatText('')).toEqual([]);
  });

  it('is only the address when that is all the message holds', () => {
    expect(splitChatText('www.example.com')).toEqual([{ kind: 'link', text: 'www.example.com', start: 0 }]);
  });
});
