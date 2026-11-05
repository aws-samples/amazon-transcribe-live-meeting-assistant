/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
// @vitest-environment node
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { JSDOM } from 'jsdom';
import { describe, expect, it } from 'vitest';

const FRAME = resolve(__dirname, '../../../public/strands-chat.html');

// The frame as the meeting page loads it: framed by a parent on its own origin.
// External scripts are not loaded; the inline script runs as written.
const loadFrame = () => {
  const posted = [];
  const errors = [];
  const dom = new JSDOM(readFileSync(FRAME, 'utf8'), {
    url: 'https://lma.example.com/strands-chat.html',
    runScripts: 'dangerously',
    beforeParse(window) {
      Object.defineProperty(window, 'parent', {
        value: { postMessage: (message, origin) => posted.push({ message, origin }) },
      });
      window.addEventListener('error', (event) => errors.push(event.error || event.message));
    },
  });
  return { dom, posted, errors };
};

describe('strands-chat.html', () => {
  it('asks the page for its chat buttons when it loads', () => {
    const { dom, posted, errors } = loadFrame();
    const requests = posted.filter(({ message }) => message?.type === 'STRANDS_BUTTON_CONFIG_REQUEST');
    expect(errors).toEqual([]);
    expect(requests).toHaveLength(1);
    expect(requests[0].origin).toBe('https://lma.example.com');
    dom.window.close();
  });
});
