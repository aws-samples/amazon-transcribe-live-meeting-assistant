/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Finding the web addresses in a message typed in the meeting chat.
 *
 * A chat message is shown as it was typed, and the addresses in it are shown
 * as code with a copy button, never as links to follow: the page does not send
 * a reader to an address because someone in a meeting typed it. This module
 * only decides which characters of the text are an address. Kept free of React
 * so the rules can be tested directly.
 *
 * An address is something starting `http://`, `https://` or `www.`, up to the
 * first space, quote, angle bracket or backtick. Punctuation that ends the
 * sentence around it (`.`, `,`, `!`, a closing quote ...) is not part of it,
 * nor is a closing bracket with no opening one inside the address -- the `)`
 * of "(see https://example.com)". Only http and https addresses count, and
 * none longer than MAX_CHAT_LINK_LENGTH characters.
 */

/** The longest address treated as one; longer text is left as plain text. */
export const MAX_CHAT_LINK_LENGTH = 2048;

// A candidate: the scheme or `www.`, then everything up to a character that
// cannot be in an address. Not preceded by a letter, digit or one of the
// characters that would make it the middle of something else
// ("xhttp://", "user@www.example.com", "a.www.example.com").
const CANDIDATE = /(?<![\p{L}\p{N}_@.\-/])(?:https?:\/\/|www\.)[^\s<>"`\p{Cc}]+/giu;

// Punctuation that ends a sentence or a quotation rather than an address.
const TRAILING_PUNCTUATION = /[.,;:!?'*_~‘’“”…]$/u;

const CLOSING = { ')': '(', ']': '[', '}': '{' };

const count = (text, char) => text.split(char).length - 1;

/** The candidate without what ends the sentence around it. */
const trimAddress = (candidate) => {
  let address = candidate;
  for (;;) {
    const last = address.slice(-1);
    if (TRAILING_PUNCTUATION.test(address)) {
      address = address.slice(0, -1);
    } else if (CLOSING[last] && count(address, last) > count(address, CLOSING[last])) {
      address = address.slice(0, -1);
    } else {
      return address;
    }
  }
};

/** Whether `address` is an http or https address with a host. */
const isWebAddress = (address) => {
  if (!address || address.length > MAX_CHAT_LINK_LENGTH) return false;
  const absolute = /^www\./i.test(address) ? `https://${address}` : address;
  try {
    const url = new URL(absolute);
    // A bare "www." or "https://." has no real host.
    return (url.protocol === 'http:' || url.protocol === 'https:') && /[\p{L}\p{N}]/u.test(url.hostname);
  } catch {
    return false;
  }
};

/**
 * The addresses in `text`, as { start, end, url } in order, `url` being the
 * characters of `text` from start to end (as typed: a `www.` address keeps no
 * scheme added).
 */
export const findChatLinks = (text) => {
  const source = typeof text === 'string' ? text : '';
  const links = [];
  source.replace(CANDIDATE, (candidate, offset) => {
    const url = trimAddress(candidate);
    if (isWebAddress(url)) links.push({ start: offset, end: offset + url.length, url });
    return candidate;
  });
  return links;
};

/**
 * `text` cut into its plain runs and its addresses, in order:
 * [{ kind: 'text' | 'link', text, start }], `start` being where the piece
 * begins in `text`. Joining the pieces' text gives `text` back.
 */
export const splitChatText = (text) => {
  const source = typeof text === 'string' ? text : '';
  const pieces = [];
  let cursor = 0;
  findChatLinks(source).forEach(({ start, end, url }) => {
    if (start > cursor) pieces.push({ kind: 'text', text: source.slice(cursor, start), start: cursor });
    pieces.push({ kind: 'link', text: url, start });
    cursor = end;
  });
  if (cursor < source.length) pieces.push({ kind: 'text', text: source.slice(cursor), start: cursor });
  return pieces;
};
