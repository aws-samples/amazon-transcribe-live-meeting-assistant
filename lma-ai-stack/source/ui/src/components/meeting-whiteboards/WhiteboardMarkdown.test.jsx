/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * A panel's Markdown: GitHub-flavoured tables render as tables, raw HTML is
 * shown as text rather than parsed, pictures are not rendered, and links open
 * in a new tab.
 */
import React from 'react';
import { describe, expect, it } from 'vitest';
import { render } from '@testing-library/react';

import WhiteboardMarkdown from './WhiteboardMarkdown';

describe('WhiteboardMarkdown', () => {
  it('renders a pipe table as a table', () => {
    const { container } = render(
      <WhiteboardMarkdown>{'| Option | Price |\n| --- | --- |\n| A | $10 |\n| B | $12 |'}</WhiteboardMarkdown>,
    );
    const table = container.querySelector('table');
    expect(table).not.toBeNull();
    expect([...table.querySelectorAll('th')].map((th) => th.textContent)).toEqual(['Option', 'Price']);
    expect(table.querySelectorAll('tbody tr')).toHaveLength(2);
  });

  it('keeps raw HTML as text and renders no pictures', () => {
    const { container } = render(
      <WhiteboardMarkdown>{'<b>bold</b> and ![chart](https://example.com/x.png)'}</WhiteboardMarkdown>,
    );
    expect(container.querySelector('b')).toBeNull();
    expect(container.querySelector('img')).toBeNull();
    expect(container.textContent).toContain('<b>bold</b>');
  });

  it('opens links in a new tab without the opener', () => {
    const { container } = render(<WhiteboardMarkdown>[docs](https://example.com)</WhiteboardMarkdown>);
    const link = container.querySelector('a');
    expect(link.getAttribute('target')).toBe('_blank');
    expect(link.getAttribute('rel')).toBe('noopener noreferrer');
  });
});
