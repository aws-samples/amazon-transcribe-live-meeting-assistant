/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
// @vitest-environment node
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { JSDOM } from 'jsdom';
import { describe, expect, it } from 'vitest';

const PUBLIC = resolve(__dirname, '../../../public');
const FRAME = resolve(PUBLIC, 'strands-chat.html');
const FRAME_SCRIPT_TAG = '<script src="js/strands-chat.js"></script>';

// The page with its own script, js/strands-chat.js, placed where the page loads
// it. The vendored libraries are not loaded (see `markdown` below).
const frameHtml = () => {
  const html = readFileSync(FRAME, 'utf8');
  const script = readFileSync(resolve(PUBLIC, 'js/strands-chat.js'), 'utf8');
  return html.replace(FRAME_SCRIPT_TAG, () => `<script>${script}</script>`);
};

// The frame as the meeting page loads it: framed by a parent on its own origin.
const loadFrame = ({ markdown = false } = {}) => {
  const posted = [];
  const errors = [];
  const dom = new JSDOM(frameHtml(), {
    url: 'https://lma.example.com/strands-chat.html',
    runScripts: 'dangerously',
    beforeParse(window) {
      Object.defineProperty(window, 'parent', {
        value: { postMessage: (message, origin) => posted.push({ message, origin }) },
      });
      window.addEventListener('error', (event) => errors.push(event.error || event.message));
      if (markdown) {
        // js/render-markdown.js is not loaded here; stand in for it with the
        // fallback it signals when its libraries are missing (plain text).
        Object.defineProperty(window, 'renderMarkdownSafe', { value: () => null });
        Object.defineProperty(window, 'withKeyframePlaceholders', { value: (text) => text });
      }
    },
  });
  return { dom, posted, errors };
};

describe('strands-chat.html', () => {
  // The web app's Content-Security-Policy allows scripts and stylesheets from
  // its own origin only, so the page keeps its code and styles in files.
  it('loads its script and styles from files, with nothing inline', () => {
    const html = readFileSync(FRAME, 'utf8');
    expect(html).toContain(FRAME_SCRIPT_TAG);
    expect(html).toContain('<link rel="stylesheet" href="css/strands-chat.css">');
    expect(html).not.toMatch(/<script(?![^>]*\bsrc=)[^>]*>/i);
    expect(html).not.toMatch(/<style\b/i);
    expect(html).not.toMatch(/\son[a-z]+\s*=/i);
    expect(html).not.toMatch(/\sstyle\s*=/i);
  });

  it('asks the page for its chat buttons when it loads', () => {
    const { dom, posted, errors } = loadFrame();
    const requests = posted.filter(({ message }) => message?.type === 'STRANDS_BUTTON_CONFIG_REQUEST');
    expect(errors).toEqual([]);
    expect(requests).toHaveLength(1);
    expect(requests[0].origin).toBe('https://lma.example.com');
    dom.window.close();
  });

  describe('a streamed answer', () => {
    const MESSAGE_ID = 'chat-1';

    const stream = () => {
      const frame = loadFrame({ markdown: true });
      const { window } = frame.dom;
      window.subscribeToTokenStream(MESSAGE_ID);
      const send = (token) =>
        window.dispatchEvent(
          new window.MessageEvent('message', {
            origin: 'https://lma.example.com',
            data: { type: 'STRANDS_TOKEN_MESSAGE', token: { MessageId: MESSAGE_ID, IsComplete: false, ...token } },
          }),
        );
      const shown = () => window.document.getElementById(`streaming-message-${MESSAGE_ID}`).textContent;
      return { ...frame, send, shown };
    };

    it('shows chunks in the order they were published, whatever order they arrive in', () => {
      const { dom, send, shown, errors } = stream();
      send({ Sequence: 0, Token: "I can't use the DeepWi" });
      send({ Sequence: 2, Token: 'ool because' });
      expect(shown()).toBe("I can't use the DeepWiool because");
      send({ Sequence: 1, Token: 'ki t' });
      expect(shown()).toBe("I can't use the DeepWiki tool because");
      expect(errors).toEqual([]);
      dom.window.close();
    });

    it('keeps a chunk received twice once', () => {
      const { dom, send, shown } = stream();
      send({ Sequence: 0, Token: 'offline' });
      send({ Sequence: 1, Token: ' sync' });
      send({ Sequence: 1, Token: ' sync' });
      expect(shown()).toBe('offline sync');
      dom.window.close();
    });

    it('does not let thinking steps, which share the numbering, displace answer text', () => {
      const { dom, send, shown } = stream();
      send({ Sequence: 0, Token: '', ThinkingStep: { Type: 'tool_use', ToolName: 'search' } });
      send({ Sequence: 0, Token: 'offline' });
      send({ Sequence: 1, Token: ' sync' });
      expect(shown()).toBe('offline sync');
      dom.window.close();
    });

    it('places chunks without a Sequence after the numbered ones, in the order received', () => {
      const { dom, send, shown } = stream();
      send({ Token: ' and' });
      send({ Sequence: 0, Token: 'offline sync' });
      send({ Token: ' more' });
      expect(shown()).toBe('offline sync and more');
      dom.window.close();
    });

    it('replaces the streamed text with the answer the completion marker carries', () => {
      const { dom, send, shown } = stream();
      send({ Sequence: 0, Token: 'o' });
      send({ Sequence: 2, Token: ' sync' });
      send({ Sequence: 3, Token: 'offline sync', IsComplete: true });
      expect(shown()).toBe('offline sync');
      dom.window.close();
    });

    it('keeps the assembled text when the completion marker carries none', () => {
      const { dom, send, shown } = stream();
      send({ Sequence: 1, Token: ' sync' });
      send({ Sequence: 0, Token: 'offline' });
      send({ Sequence: 2, Token: '', IsComplete: true });
      expect(shown()).toBe('offline sync');
      dom.window.close();
    });
  });
});
