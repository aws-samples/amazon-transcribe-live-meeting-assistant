/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Finding the web addresses in a message typed in the meeting chat.
 *
 * The twin of extract_links in lma_chat_links, the configuration Lambda
 * layer's module that FetchTranscript uses to list the links shared in a
 * meeting's chat for its summary. Both apply the rules below, and
 * test-fixtures/chat-links.json at the repository root holds both to them:
 * extractChatLinks returns exactly what extract_links does.
 *
 * - A candidate starts with `http://`, `https://` or `www.` (any case), not
 *   directly after an ASCII letter or digit or one of `@ . / : -`, and runs to
 *   the next whitespace, `<`, `>` or `"`.
 * - Trailing punctuation is trimmed (`. , ; : ! ? ' " * _ ~`, the curly
 *   quotes ‘ ’ “ ” that chat and mail clients put in place of straight ones,
 *   the backtick and the ellipsis), and so is a trailing `)`, `]` or `}` with
 *   no opening partner in the address, repeatedly.
 * - The address is the candidate with `https://` before a `www.` one, the
 *   scheme in lower case and each backtick left inside it written `%60`.
 * - Only an http or https address with a host is kept, and none longer than
 *   MAX_CHAT_LINK_LENGTH characters.
 *
 * On the meeting page an address is shown as typed, as code with a button that
 * copies the address, and never as a link to follow (see ChatSegment.jsx).
 * Kept free of React so the rules can be tested directly.
 */

/** The longest address kept, in characters, once written as an address. */
export const MAX_CHAT_LINK_LENGTH = 2048;

// What Python's \s matches in a str pattern (str.isspace), which is not quite
// JavaScript's \s: it adds U+001C-U+001F and U+0085, and leaves out U+FEFF.
const SPACE = '\\t\\n\\v\\f\\r\\u001c-\\u001f \\u0085\\u00a0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000';

// A candidate. The lookbehind keeps "user@www.example.com" and the middle of
// another word from starting one. `u` gives the same Unicode case folding as
// Python's IGNORECASE.
const CANDIDATE = new RegExp(`(?<![A-Za-z0-9@./:\\-])(?:https?://|www\\.)[^${SPACE}<>"]+`, 'giu');

// An http or https address with a host: an optional user part, at least one
// host character, an optional port, then the end or a path, query or fragment.
// \p{Nd} is what Python's \d matches.
const VALID = new RegExp(`^https?://(?:[^/?#@${SPACE}]*@)?[^/?#@:${SPACE}]+(?::\\p{Nd}*)?(?:[/?#]|$)`, 'iu');

const TRAILING_PUNCTUATION = new Set([...'.,;:!?\'"*_~…‘’“”`']);
const CLOSING = { ')': '(', ']': '[', '}': '{' };

const count = (text, char) => text.split(char).length - 1;

/** The candidate without what ends the sentence around it. */
const trim = (candidate) => {
  let url = candidate;
  while (url) {
    const last = url.slice(-1);
    const opener = CLOSING[last];
    if (TRAILING_PUNCTUATION.has(last) || (opener && count(url, last) > count(url, opener))) {
      url = url.slice(0, -1);
    } else {
      break;
    }
  }
  return url;
};

/** The address a trimmed candidate stands for. */
const asAddress = (typed) => {
  const url = typed.slice(0, 4).toLowerCase() === 'www.' ? `https://${typed}` : typed;
  const at = url.indexOf('://');
  const written = at < 0 ? url : url.slice(0, at).toLowerCase() + url.slice(at);
  return written.replaceAll('`', '%60');
};

/**
 * Every address in `text`, each where it occurs (so one typed twice is listed
 * twice): [{ start, end, url, address }], `url` being the characters of `text`
 * from start to end, as typed, and `address` the address they stand for.
 */
export const findChatLinks = (text) => {
  if (typeof text !== 'string' || !text) return [];
  const links = [];
  [...text.matchAll(CANDIDATE)].forEach((match) => {
    const url = trim(match[0]);
    const address = asAddress(url);
    if (VALID.test(address) && address.length <= MAX_CHAT_LINK_LENGTH) {
      links.push({ start: match.index, end: match.index + url.length, url, address });
    }
  });
  return links;
};

/** The addresses in `text`, in order of first appearance, each once: lma_chat_links.extract_links. */
export const extractChatLinks = (text) => [...new Set(findChatLinks(text).map((link) => link.address))];

/**
 * `text` cut into its plain runs and its addresses, in order:
 * [{ kind: 'text' | 'link', text, start }], a link also carrying its
 * `address`, `start` being where the piece begins in `text`. Joining the
 * pieces' text gives `text` back.
 */
export const splitChatText = (text) => {
  const source = typeof text === 'string' ? text : '';
  const pieces = [];
  let cursor = 0;
  findChatLinks(source).forEach(({ start, end, url, address }) => {
    if (start > cursor) pieces.push({ kind: 'text', text: source.slice(cursor, start), start: cursor });
    pieces.push({ kind: 'link', text: url, start, address });
    cursor = end;
  });
  if (cursor < source.length) pieces.push({ kind: 'text', text: source.slice(cursor), start: cursor });
  return pieces;
};
