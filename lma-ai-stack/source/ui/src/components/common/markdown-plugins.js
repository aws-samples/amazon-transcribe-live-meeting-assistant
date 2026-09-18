/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/*
 * Shared rehype plugin chain for every <ReactMarkdown> that renders text
 * produced by a model or typed by a user -- meeting summaries, transcript
 * segments, translations and meeting-query answers.
 *
 * One deliberate exception: the question label in MeetingsQueryLayout's
 * ValueWithLabel renders with no rehypePlugins at all. Without `rehype-raw`,
 * react-markdown escapes HTML in the source instead of parsing it, so any markup
 * in the label is shown as literal text. Adding this chain there would turn that
 * escaping into parse-then-filter, which is weaker, so it is left alone.
 *
 * `rehype-raw` is kept because those strings legitimately contain small bits of
 * inline HTML (line breaks, emphasis) that authors and prompt templates rely on.
 * `rehype-sanitize` runs immediately after it, so the invariant across all of
 * these surfaces is that the HTML tree handed to React has already been reduced
 * to the GitHub-flavoured allowlist in `defaultSchema` -- the tags and
 * attributes markdown itself produces.
 *
 * `defaultSchema` is used unmodified. It already permits `className` matching
 * /^language-./ on `code` (hast-util-sanitize lib/schema.js), which is what
 * fenced code blocks emit, so syntax-highlighting hints survive without an
 * override here.
 *
 * Order matters: raw HTML has to be parsed into nodes before it can be filtered,
 * so `rehypeRaw` must stay first in the array.
 */
import rehypeRaw from 'rehype-raw';
import rehypeSanitize, { defaultSchema } from 'rehype-sanitize';

/**
 * rehype plugin chain to spread onto <ReactMarkdown rehypePlugins={...}>.
 */
const markdownRehypePlugins = [rehypeRaw, [rehypeSanitize, defaultSchema]];

export default markdownRehypePlugins;
