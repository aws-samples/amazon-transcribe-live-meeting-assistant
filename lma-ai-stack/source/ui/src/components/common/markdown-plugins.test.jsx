/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/*
 * Covers the two places LMA turns model/user text into HTML:
 *
 *   1. the shared <ReactMarkdown> rehype chain (src/components/common/markdown-plugins.js)
 *   2. renderMarkdownSafe() used by the standalone chat page
 *      (public/js/render-markdown.js, composed with the vendored marked + DOMPurify)
 *
 * Both are asserted to keep ordinary markdown formatting while reducing the HTML
 * tree to the allowed tag/attribute set, so markup embedded in the source text
 * does not survive as elements in the rendered DOM.
 */
import fs from 'fs';
import path from 'path';
import React from 'react';
import { render } from '@testing-library/react';
import ReactMarkdown from 'react-markdown';
import { describe, it, expect, beforeAll } from 'vitest';
import markdownRehypePlugins from './markdown-plugins';

const PUBLIC_JS = path.resolve(__dirname, '../../../public/js');

// Load a vendored classic script into the jsdom global, the same way a
// <script src> tag would on the real page.
const loadVendoredScript = (filename) => {
  const source = fs.readFileSync(path.join(PUBLIC_JS, filename), 'utf8');
  // eslint-disable-next-line no-new-func
  new Function(source).call(globalThis);
};

const renderMarkdown = (text) => render(<ReactMarkdown rehypePlugins={markdownRehypePlugins}>{text}</ReactMarkdown>);

describe('markdownRehypePlugins (ReactMarkdown surfaces)', () => {
  it('renders ordinary markdown formatting', () => {
    const { container } = renderMarkdown('# Heading\n\nSome **bold** and a [link](https://example.com).');

    expect(container.querySelector('h1')).not.toBeNull();
    expect(container.querySelector('strong')?.textContent).toBe('bold');
    expect(container.querySelector('a')?.getAttribute('href')).toBe('https://example.com');
  });

  it('keeps inline html that the allowlist permits', () => {
    const { container } = renderMarkdown('line one<br>line two and <em>emphasis</em>');

    expect(container.querySelector('br')).not.toBeNull();
    expect(container.querySelector('em')?.textContent).toBe('emphasis');
  });

  // Guards against over-stripping: table markup is on the allowlist and must
  // survive. This asserts nothing about sanitization on its own.
  it('keeps inline table markup that the allowlist permits', () => {
    const { container } = renderMarkdown('<table><tr><td>cell</td></tr></table>');

    expect(container.querySelector('td')?.textContent).toBe('cell');
  });

  it('renders markdown without script elements', () => {
    const { container } = renderMarkdown('summary text\n\n<script>window.marker = 1;</script>');

    expect(container.querySelector('script')).toBeNull();
    expect(globalThis.marker).toBeUndefined();
  });

  it('renders markdown without event handler attributes', () => {
    const { container } = renderMarkdown('<p onclick="window.marker = 1">paragraph</p>');

    const paragraph = container.querySelector('p');
    expect(paragraph?.textContent).toBe('paragraph');
    expect(paragraph?.getAttribute('onclick')).toBeNull();
  });

  it('renders markdown without embedding frames or objects', () => {
    const { container } = renderMarkdown('<iframe src="https://example.com"></iframe><object data="x"></object>');

    expect(container.querySelector('iframe')).toBeNull();
    expect(container.querySelector('object')).toBeNull();
  });

  it('drops non-http link protocols while keeping the link text', () => {
    const { container } = renderMarkdown('<a href="javascript:window.marker=1">click</a>');

    expect(container.textContent).toContain('click');
    expect(container.querySelector('a')?.getAttribute('href')).toBeNull();
  });

  it('keeps language class names on fenced code blocks', () => {
    const { container } = renderMarkdown('```js\nconst a = 1;\n```');

    expect(container.querySelector('code')?.getAttribute('class')).toBe('language-js');
  });
});

describe('renderMarkdownSafe (standalone chat page)', () => {
  beforeAll(() => {
    loadVendoredScript('marked.min.js');
    loadVendoredScript('purify.min.js');
    loadVendoredScript('render-markdown.js');
  });

  const renderToDom = (text) => {
    const host = document.createElement('div');
    host.innerHTML = globalThis.renderMarkdownSafe(text);
    return host;
  };

  it('exposes the vendored libraries and the renderer as globals', () => {
    expect(typeof globalThis.marked?.parse).toBe('function');
    expect(typeof globalThis.DOMPurify?.sanitize).toBe('function');
    expect(typeof globalThis.renderMarkdownSafe).toBe('function');
  });

  it('returns an empty string for empty input', () => {
    expect(globalThis.renderMarkdownSafe('')).toBe('');
    expect(globalThis.renderMarkdownSafe(undefined)).toBe('');
  });

  it('renders ordinary markdown formatting', () => {
    const host = renderToDom('## Answer\n\n- first\n- second\n\n**emphasis** and `code`');

    expect(host.querySelector('h2')?.textContent).toBe('Answer');
    expect(host.querySelectorAll('li')).toHaveLength(2);
    expect(host.querySelector('strong')?.textContent).toBe('emphasis');
    expect(host.querySelector('code')?.textContent).toBe('code');
  });

  it('renders markdown links with their href intact', () => {
    const host = renderToDom('see [the docs](https://example.com/docs)');

    expect(host.querySelector('a')?.getAttribute('href')).toBe('https://example.com/docs');
  });

  /*
   * The next three cases exist because every entry in FORBID_ATTR / FORBID_TAGS
   * needs a companion assertion that it did not catch a legitimate construct.
   * GFM is on by default in marked and this page overrides nothing, so task
   * lists and aligned tables are output the renderer really produces.
   */
  it('renders task lists with their checked state intact', () => {
    const host = renderToDom('- [ ] todo item\n- [x] done item');

    const boxes = host.querySelectorAll('input[type="checkbox"]');
    expect(boxes).toHaveLength(2);
    // The two items differ only by `checked`, so losing it would make an
    // unchecked and a checked task render identically.
    expect(boxes[0].hasAttribute('checked')).toBe(false);
    expect(boxes[1].hasAttribute('checked')).toBe(true);
    // A rendered task list is never interactive.
    expect(boxes[0].hasAttribute('disabled')).toBe(true);
    expect(boxes[1].hasAttribute('disabled')).toBe(true);
    expect(host.textContent).toContain('todo item');
    expect(host.textContent).toContain('done item');
  });

  it('renders table column alignment', () => {
    const host = renderToDom('| a | b |\n| :-: | --: |\n| 1 | 2 |');

    const headers = host.querySelectorAll('th');
    expect(headers).toHaveLength(2);
    // marked emits alignment as an `align` attribute, not inline style, so it
    // must survive FORBID_ATTR: ['style'].
    expect(headers[0].getAttribute('align')).toBe('center');
    expect(headers[1].getAttribute('align')).toBe('right');
  });

  it('renders fenced code blocks with their language class', () => {
    const host = renderToDom('```js\nconst a = 1;\n```');

    const code = host.querySelector('pre code');
    expect(code?.getAttribute('class')).toBe('language-js');
    expect(code?.textContent).toContain('const a = 1;');
  });

  it('renders markdown without script elements', () => {
    const host = renderToDom('here is the summary\n\n<script>globalThis.chatMarker = 1;</script>');

    expect(host.querySelector('script')).toBeNull();
    expect(host.textContent).toContain('here is the summary');
    expect(globalThis.chatMarker).toBeUndefined();
  });

  it('renders markdown without event handler attributes', () => {
    const host = renderToDom('<div onmouseover="globalThis.chatMarker = 1">hover</div>');

    const div = host.querySelector('div');
    expect(div?.textContent).toBe('hover');
    expect(div?.getAttribute('onmouseover')).toBeNull();
  });

  it('renders markdown without embedding frames or objects', () => {
    const host = renderToDom('<iframe src="https://example.com"></iframe><embed src="x">');

    expect(host.querySelector('iframe')).toBeNull();
    expect(host.querySelector('embed')).toBeNull();
  });

  it('renders markdown without svg elements', () => {
    const host = renderToDom('<svg><circle r="10"></circle></svg>');

    expect(host.querySelector('svg')).toBeNull();
  });

  it('drops non-http link protocols while keeping the link text', () => {
    const host = renderToDom('<a href="javascript:globalThis.chatMarker=1">click</a>');

    expect(host.textContent).toContain('click');
    expect(host.querySelector('a')?.getAttribute('href')).toBeNull();
  });

  it('renders links without target or rel attributes', () => {
    const host = renderToDom('<a href="https://example.com" target="_blank" rel="opener">link</a>');

    const link = host.querySelector('a');
    expect(link?.getAttribute('href')).toBe('https://example.com');
    expect(link?.getAttribute('target')).toBeNull();
    expect(link?.getAttribute('rel')).toBeNull();
  });

  it('renders markdown without interactive form controls', () => {
    const host = renderToDom('<form action="https://example.com"><input name="a"><button>go</button></form>');

    expect(host.querySelector('form')).toBeNull();
    expect(host.querySelector('input')).toBeNull();
    expect(host.querySelector('button')).toBeNull();
  });

  it('renders markdown without style elements', () => {
    // Nested rather than at the start of the input on purpose. The HTML parser
    // hoists a leading <style> into <head>, which masks the element surviving in
    // the positions model output actually puts it in -- mid-paragraph, or inside
    // a list item or table cell.
    const host = renderToDom('Here are the action items: <style>p{display:none}</style>');

    expect(host.querySelector('style')).toBeNull();
    expect(host.textContent).toContain('Here are the action items:');
    // The declaration text is dropped with the element rather than being left
    // behind as visible text.
    expect(host.textContent).not.toContain('display:none');
  });

  it('renders markdown without style attributes', () => {
    const host = renderToDom('<p style="position:fixed;top:0">styled</p>');

    const paragraph = host.querySelector('p');
    expect(paragraph?.textContent).toBe('styled');
    expect(paragraph?.getAttribute('style')).toBeNull();
  });

  it('signals to the caller when the sanitizer is unavailable', () => {
    const sanitizer = globalThis.DOMPurify;
    try {
      globalThis.DOMPurify = undefined;
      expect(globalThis.renderMarkdownSafe('some **text**')).toBeNull();
    } finally {
      globalThis.DOMPurify = sanitizer;
    }
  });
});
