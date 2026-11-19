/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The "Web page (.html)" downloads: the summary or the transcript as one
 * self-contained HTML file, readable in any browser and printable.
 *
 * Everything the page needs is in the file: its styles are in one <style>
 * element, and each picture of the shared screen is embedded as a `data:` URI.
 * The page has no script, and loads no font, stylesheet or picture from
 * anywhere; its Content-Security-Policy says so to the browser as well
 * (`default-src 'none'`), so it stays that way whatever the summary's text
 * says. The only addresses in it are the links the summary itself holds
 * (`http:`, `https:` and `mailto:` ones), which open in a new tab.
 *
 * A summary section is rendered with the parser the Summary tab uses
 * (react-markdown, CommonMark), through the Summary tab's sanitizer schema, but
 * without its raw-HTML step: HTML written in the summary's text is shown as
 * the text it is, never as markup. React writes every piece of text escaped.
 * A key frame (`![alt](keyframe:<FrameId>)`) is the only picture: it is shown
 * from the thumbnail the caller fetched, or as its caption in brackets, as in
 * the other downloads. Any other picture is its alt text, and a link to
 * anything but `http:`, `https:` or `mailto:` (a `javascript:` or `data:`
 * link, say) is its text alone.
 *
 * The page lists its sections at the top, as links to each, and each section
 * is a <details> element, open at first, that the reader can close. Loaded on
 * demand by download-func.js, so react-dom/server is not in the page's bundle.
 */
import React from 'react';
import PropTypes from 'prop-types';
import { renderToStaticMarkup } from 'react-dom/server';
import ReactMarkdown, { defaultUrlTransform } from 'react-markdown';
import rehypeSanitize from 'rehype-sanitize';

import { summarySchema } from '../summary-markdown/SummaryMarkdown';

const KEYFRAME_SCHEME = 'keyframe:';
const FRAME_ID = /^[A-Za-z0-9_-]{1,64}$/;
// The link addresses a downloaded page keeps; anything else is written as its text.
const SAFE_LINK = /^(?:https?:\/\/|mailto:)/i;

/**
 * The page's Content-Security-Policy: no script, nothing fetched from
 * anywhere, pictures only from `data:` URIs, and styles only from the page.
 */
export const HTML_EXPORT_CSP =
  "default-src 'none'; img-src data:; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'";

// Print-friendly, in the system's own fonts (no font is loaded).
export const HTML_EXPORT_CSS = `
:root { color-scheme: light; }
* { box-sizing: border-box; }
body { margin: 0; background: #f4f4f4; color: #16191f;
  font: 15px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif; }
main { max-width: 52rem; margin: 2rem auto; padding: 2rem 2.5rem; background: #fff;
  border: 1px solid #e0e0e0; border-radius: 6px; }
h1 { font-size: 1.7rem; line-height: 1.25; margin: 0 0 1rem; }
h2 { font-size: 1.2rem; margin: 0; display: inline; }
h3, h4, h5, h6 { font-size: 1rem; margin: 1.1rem 0 0.4rem; }
dl.facts { display: grid; grid-template-columns: max-content 1fr; gap: 0.2rem 1rem; margin: 0 0 1.5rem; }
dl.facts dt { font-weight: 600; color: #414d5c; }
dl.facts dd { margin: 0; overflow-wrap: anywhere; }
nav.toc { border: 1px solid #e0e0e0; border-radius: 4px; padding: 0.75rem 1.25rem; margin: 0 0 1.5rem; background: #fafafa; }
nav.toc p { margin: 0 0 0.4rem; font-weight: 600; }
nav.toc ol { margin: 0; padding-left: 1.4rem; }
section { margin: 0 0 1.25rem; border-top: 1px solid #e0e0e0; padding-top: 0.9rem; }
details > summary { cursor: pointer; list-style-position: outside; margin-bottom: 0.5rem; }
a { color: #0972d3; overflow-wrap: anywhere; }
code { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 0.9em;
  background: #f2f3f3; padding: 0 0.2em; border-radius: 3px; }
pre { background: #f2f3f3; padding: 0.75rem; overflow-x: auto; white-space: pre-wrap; }
pre code { background: none; padding: 0; }
blockquote { margin: 0.5rem 0; padding-left: 1rem; border-left: 3px solid #d1d5db; color: #414d5c; }
img.keyframe { display: block; max-width: 100%; height: auto; margin: 0.5rem 0;
  border: 1px solid #d1d5db; border-radius: 3px; }
.segment { margin: 0 0 0.45rem; white-space: pre-wrap; }
.segment .time { color: #5f6b7a; }
footer { margin-top: 2rem; color: #5f6b7a; font-size: 0.85rem; }
@media print {
  body { background: #fff; font-size: 11pt; }
  main { margin: 0; padding: 0; border: 0; max-width: none; }
  nav.toc { break-inside: avoid; }
  details > summary { list-style: none; }
  details > summary::-webkit-details-marker { display: none; }
  h1, h2, h3, h4, h5, h6, summary { break-after: avoid; }
  img.keyframe { break-inside: avoid; max-height: 45vh; width: auto; }
  a { color: inherit; }
}
`;

/** Bytes (a Uint8Array) as base64, in chunks so a large picture does not overflow the call stack. */
export const bytesToBase64 = (bytes) => {
  let binary = '';
  const CHUNK = 0x8000;
  for (let i = 0; i < bytes.length; i += CHUNK) {
    binary += String.fromCharCode(...bytes.subarray(i, i + CHUNK));
  }
  return btoa(binary);
};

/** A thumbnail ({ data: Uint8Array, ... }, as fetchKeyframeThumbnails returns it) as a JPEG data: URI. */
export const thumbnailDataUri = (image) =>
  image?.data instanceof Uint8Array ? `data:image/jpeg;base64,${bytesToBase64(image.data)}` : null;

/** The address a link keeps, or null when the page writes the link as its text. */
export const safeLinkHref = (href) => {
  if (typeof href !== 'string') return null;
  const value = href.trim();
  return SAFE_LINK.test(value) ? value : null;
};

// Keeps a `keyframe:` picture's source for the img component below; every
// other address as react-markdown treats it, which empties a `javascript:` or
// `data:` one (the `a` and `img` components refuse what is left).
const urlTransform = (url, key, node) => {
  if (key === 'src' && node?.tagName === 'img') {
    return typeof url === 'string' && url.startsWith(KEYFRAME_SCHEME) ? url : '';
  }
  return defaultUrlTransform(url);
};

// A summary's own headings sit under the page's section headings (h2).
const SHIFTED_HEADINGS = { h1: 'h3', h2: 'h4', h3: 'h5', h4: 'h6', h5: 'h6', h6: 'h6' };

const componentsFor = (thumbnails) => {
  /* eslint-disable react/prop-types, no-unused-vars -- react-markdown's own props, and its hast `node` */
  const Link = ({ node, href, children }) => {
    const safe = safeLinkHref(href);
    if (!safe) return <span>{children}</span>;
    return (
      <a href={safe} target="_blank" rel="noopener noreferrer">
        {children}
      </a>
    );
  };
  const Image = ({ node, src, alt }) => {
    const caption = alt || 'Shared screen';
    if (typeof src === 'string' && src.startsWith(KEYFRAME_SCHEME)) {
      const id = src.slice(KEYFRAME_SCHEME.length);
      const uri = FRAME_ID.test(id) ? thumbnailDataUri(thumbnails[id]) : null;
      if (uri) {
        const { width, height } = thumbnails[id];
        return <img className="keyframe" src={uri} alt={caption} title={caption} width={width} height={height} />;
      }
      return <span>({caption})</span>;
    }
    // Not a frame of this meeting: its alt text, as on the Summary tab.
    return alt ? <span>{alt}</span> : null;
  };
  const headings = Object.fromEntries(
    Object.entries(SHIFTED_HEADINGS).map(([from, Tag]) => {
      const Heading = ({ node, children }) => <Tag>{children}</Tag>;
      return [from, Heading];
    }),
  );
  /* eslint-enable react/prop-types, no-unused-vars */
  return { a: Link, img: Image, ...headings };
};

/**
 * A remark plugin: HTML written in the Markdown (an `html` node, inline or as
 * a block) becomes text, so the page shows it as it was typed and React writes
 * it escaped. Without it the raw HTML would be dropped, and the text inside a
 * tag kept, which loses what the summary said.
 */
export const remarkHtmlAsText = () => (tree) => {
  const visit = (node) => {
    (node.children || []).forEach((child, index) => {
      if (child.type !== 'html') {
        visit(child);
        return;
      }
      const text = { type: 'text', value: child.value, position: child.position };
      // A block of HTML is a paragraph of its text; HTML within a line is text in that line.
      const block = node.type === 'root' || node.type === 'blockquote' || node.type === 'listItem';
      // eslint-disable-next-line no-param-reassign -- a remark plugin rewrites the tree in place
      node.children[index] = block ? { type: 'paragraph', children: [text], position: child.position } : text;
    });
  };
  visit(tree);
};

const Markdown = ({ markdown, components }) => (
  <ReactMarkdown
    remarkPlugins={[remarkHtmlAsText]}
    rehypePlugins={[[rehypeSanitize, summarySchema]]}
    urlTransform={urlTransform}
    components={components}
  >
    {markdown}
  </ReactMarkdown>
);

Markdown.propTypes = {
  markdown: PropTypes.string.isRequired,
  components: PropTypes.objectOf(PropTypes.func).isRequired,
};

const FACT_SHAPE = PropTypes.shape({ label: PropTypes.string, value: PropTypes.string });

const Page = ({ title, facts, children }) => (
  <html lang="en">
    <head>
      <meta charSet="utf-8" />
      <meta httpEquiv="Content-Security-Policy" content={HTML_EXPORT_CSP} />
      <meta name="viewport" content="width=device-width, initial-scale=1" />
      <meta name="referrer" content="no-referrer" />
      <title>{title}</title>
      {/* A constant of this module, not text from the meeting. */}
      {/* eslint-disable-next-line react/no-danger */}
      <style dangerouslySetInnerHTML={{ __html: HTML_EXPORT_CSS }} />
    </head>
    <body>
      <main>
        <h1>{title}</h1>
        {facts.length > 0 && (
          <dl className="facts">
            {facts.map(({ label, value }) => (
              <React.Fragment key={label}>
                <dt>{label}</dt>
                <dd>{value}</dd>
              </React.Fragment>
            ))}
          </dl>
        )}
        {children}
        <footer>Downloaded from Live Meeting Assistant.</footer>
      </main>
    </body>
  </html>
);

Page.propTypes = {
  title: PropTypes.string.isRequired,
  facts: PropTypes.arrayOf(FACT_SHAPE).isRequired,
  children: PropTypes.node.isRequired,
};

const asDocument = (element) => `<!DOCTYPE html>\n${renderToStaticMarkup(element)}\n`;

/** The id of the page's n-th section (from 1), which its entry in the contents links to. */
export const sectionAnchor = (n) => `section-${n}`;

/**
 * The summary as an HTML document (a string). `sections` are [{ title,
 * markdown }], in the order the page lists them; `consentMarkdown` is the
 * consent record's body, written as a last section called Consent, when the
 * meeting has one; `thumbnails` are the key frames' pictures by FrameId.
 */
export const renderSummaryHtml = ({ title, facts = [], sections = [], consentMarkdown = '', thumbnails = {} }) => {
  const all = [...sections];
  if (consentMarkdown) all.push({ title: 'Consent', markdown: consentMarkdown });
  const components = componentsFor(thumbnails || {});
  return asDocument(
    <Page title={title} facts={facts}>
      {all.length > 1 && (
        <nav className="toc" aria-label="Contents">
          <p>Contents</p>
          <ol>
            {all.map((section, index) => (
              // eslint-disable-next-line react/no-array-index-key -- a hand-written summary can repeat a heading
              <li key={index}>
                <a href={`#${sectionAnchor(index + 1)}`}>{section.title}</a>
              </li>
            ))}
          </ol>
        </nav>
      )}
      {all.map((section, index) => (
        // eslint-disable-next-line react/no-array-index-key -- as above
        <section key={index} id={sectionAnchor(index + 1)}>
          <details open>
            <summary>
              <h2>{section.title}</h2>
            </summary>
            <Markdown markdown={String(section.markdown ?? '')} components={components} />
          </details>
        </section>
      ))}
    </Page>,
  );
};

/**
 * The transcript as an HTML document (a string): a paragraph per row, the
 * speaker in bold and the time after it, as the other transcript downloads
 * write it. Each row's text is written as plain text. `rows` are
 * [{ speaker, stamp, text }].
 */
export const renderTranscriptHtml = ({ title, facts = [], rows = [], consentMarkdown = '' }) => {
  const components = componentsFor({});
  return asDocument(
    <Page title={title} facts={facts}>
      <section id={sectionAnchor(1)}>
        {rows.map((row, index) => (
          // eslint-disable-next-line react/no-array-index-key -- rows are a fixed list, in time order
          <p className="segment" key={index}>
            <strong>{row.speaker}</strong> <span className="time">[{row.stamp}]</span>: {row.text}
          </p>
        ))}
      </section>
      {consentMarkdown ? (
        <section id={sectionAnchor(2)}>
          <h2>Consent</h2>
          <Markdown markdown={consentMarkdown} components={components} />
        </section>
      ) : null}
    </Page>,
  );
};
