/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The summary shows a `keyframe:` picture as that meeting's frame, read through
 * the frame lookup, and as its alt text when the frame cannot be read. The rest
 * of the summary is sanitized as every other summary surface is, and the
 * `keyframe:` scheme is accepted only as an image source.
 */
import React from 'react';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import { describe, it, expect, vi } from 'vitest';

import SummaryMarkdown from './SummaryMarkdown';

const frame = (id) => ({
  FrameId: id,
  ImageSignedUrl: `https://signed.example/${id}.jpg`,
  ThumbnailSignedUrl: `https://signed.example/${id}_t.jpg`,
});

const SLIDES = '**[00:30] Budget**\n\n![Slide at 00:30](keyframe:0000030000-ab12cd)\n\nBob covered Q3.';

describe('SummaryMarkdown', () => {
  it("shows a slide as the meeting's own frame, opening the full-size image", async () => {
    const loadFrame = vi.fn(async (_callId, id) => frame(id));
    render(
      <SummaryMarkdown callId="m-1" loadFrame={loadFrame}>
        {SLIDES}
      </SummaryMarkdown>,
    );
    const img = await screen.findByRole('img', { name: 'Slide at 00:30' });
    expect(img.getAttribute('src')).toBe('https://signed.example/0000030000-ab12cd_t.jpg');
    expect(img.closest('a').getAttribute('href')).toBe('https://signed.example/0000030000-ab12cd.jpg');
    expect(img.closest('a').getAttribute('target')).toBe('_blank');
    expect(loadFrame).toHaveBeenCalledWith('m-1', '0000030000-ab12cd');
    expect(screen.getByText('Bob covered Q3.')).toBeTruthy();
  });

  it('shows the alt text for a frame that cannot be read', async () => {
    const loadFrame = vi.fn(async () => null);
    const { container } = render(
      <SummaryMarkdown callId="m-1" loadFrame={loadFrame}>
        {SLIDES}
      </SummaryMarkdown>,
    );
    await waitFor(() => expect(loadFrame).toHaveBeenCalled());
    expect(screen.getByText('Slide at 00:30')).toBeTruthy();
    expect(container.querySelector('img')).toBeNull();
  });

  it('reads the frame again once when its link stops working, then shows the alt text', async () => {
    const loadFrame = vi.fn(async (_callId, id) => frame(id));
    const { container } = render(
      <SummaryMarkdown callId="m-1" loadFrame={loadFrame}>
        {SLIDES}
      </SummaryMarkdown>,
    );
    fireEvent.error(await screen.findByRole('img'));
    await waitFor(() => expect(loadFrame).toHaveBeenCalledTimes(2));
    fireEvent.error(await screen.findByRole('img'));
    await waitFor(() => expect(container.querySelector('img')).toBeNull());
    expect(screen.getByText('Slide at 00:30')).toBeTruthy();
  });

  it('accepts the keyframe scheme only as an image source, and still sanitizes the rest', async () => {
    const loadFrame = vi.fn();
    const { container } = render(
      <SummaryMarkdown callId="m-1" loadFrame={loadFrame}>
        {'[a link](keyframe:f1) ![bad](keyframe:not%20an%20id) <img src="x" onerror="alert(1)"> <script>x()</script>'}
      </SummaryMarkdown>,
    );
    const link = screen.getByText('a link');
    expect(link.getAttribute('href') || '').not.toContain('keyframe:');
    expect(container.querySelector('script')).toBeNull();
    expect(container.innerHTML).not.toContain('onerror');
    expect(loadFrame).not.toHaveBeenCalled();
  });

  it('renders an ordinary image as it is', () => {
    render(<SummaryMarkdown callId="m-1">![chart](https://example.com/c.png)</SummaryMarkdown>);
    expect(screen.getByRole('img', { name: 'chart' }).getAttribute('src')).toBe('https://example.com/c.png');
  });
});
