/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
/* eslint-disable react/prop-types */
import React from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';

// As the notifications drawer renders bodies: react-markdown escapes raw HTML
// (no rehype-raw here), pictures are not rendered, and links open in a new tab
// without the opener. A panel's text is written by the assistant from the
// meeting or by a person, so it never makes the browser fetch anything.
// GitHub-flavoured Markdown adds tables (the Comparison template is one), task
// lists and strikethrough; it changes how Markdown is parsed, not what HTML
// may pass, so raw HTML is still escaped.
const components = {
  a: ({ href, children }) => (
    <a href={href} target="_blank" rel="noopener noreferrer">
      {children}
    </a>
  ),
};
const remarkPlugins = [remarkGfm];

const WhiteboardMarkdown = ({ children }) => (
  <div className="whiteboard-markdown">
    <ReactMarkdown remarkPlugins={remarkPlugins} disallowedElements={['img']} unwrapDisallowed components={components}>
      {children || ''}
    </ReactMarkdown>
  </div>
);

export default WhiteboardMarkdown;
