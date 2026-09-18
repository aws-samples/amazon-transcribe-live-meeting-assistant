/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/*
 * Shared markdown renderer for the standalone chat page (strands-chat.html).
 *
 * strands-chat.html is plain HTML with no module system, so this file is a
 * classic script that registers `renderMarkdownSafe` on the global object. It
 * depends on two vendored globals loaded before it:
 *
 *   js/marked.min.js   marked v11.1.1   https://github.com/markedjs/marked
 *   js/purify.min.js   DOMPurify 3.4.15 https://github.com/cure53/DOMPurify
 *
 * Invariant: every string this function returns has been through DOMPurify, so
 * only DOMPurify's HTML profile (the tag and attribute set below) can reach the
 * DOM. Callers assign the result to `innerHTML`; if either vendored library is
 * unavailable the function returns `null` so the caller can fall back to
 * `textContent`.
 */
(function registerRenderMarkdownSafe(root) {
  // Restrict the sanitizer to its HTML profile: SVG and MathML are not used by
  // markdown output, so they stay out of the allowed set entirely.
  //
  // Nothing is added to the profile. This page configures no `marked` renderer
  // and makes no `marked.use()` call, so the default renderer's output is the
  // whole of what has to survive, and it emits no `target`, `rel` or `style`
  // attributes -- links render in this frame, as they already did.
  //
  // `target` is absent from DOMPurify's default attribute set, so it is dropped
  // without being named. `rel` and `style` are present in that set, so they are
  // named here: neither has a purpose in this content, and forbidding them makes
  // "no link-targeting or styling attributes survive" an enforced invariant
  // rather than a property of whatever the default set happens to contain.
  //
  // Interactive form controls are in DOMPurify's default HTML profile but are
  // never produced by markdown, and this page builds its own controls in code
  // rather than through this function. Forbidding them keeps the rendered
  // conversation to inert, non-interactive content.
  var SANITIZE_CONFIG = {
    USE_PROFILES: { html: true },
    FORBID_ATTR: ['rel', 'style'],
    FORBID_TAGS: ['form', 'input', 'button', 'textarea', 'select', 'option'],
  };

  /**
   * Render markdown to a sanitized HTML string.
   *
   * @param {string} text markdown source (assistant/model output)
   * @returns {string|null} sanitized HTML, '' for empty input, or null when the
   *   markdown parser or the sanitizer is unavailable.
   */
  function renderMarkdownSafe(text) {
    if (!text) {
      return '';
    }

    var parser = root.marked;
    var sanitizer = root.DOMPurify;

    // Rendering markdown without the sanitizer available is not an option, so
    // signal the caller to render the text verbatim instead.
    if (!parser || typeof parser.parse !== 'function' || !sanitizer || typeof sanitizer.sanitize !== 'function') {
      return null;
    }

    return sanitizer.sanitize(parser.parse(text), SANITIZE_CONFIG);
  }

  root.renderMarkdownSafe = renderMarkdownSafe;
})(typeof globalThis !== 'undefined' ? globalThis : window);
