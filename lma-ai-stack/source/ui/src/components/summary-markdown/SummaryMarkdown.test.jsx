/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The summary shows a `keyframe:` picture as that meeting's frame, read through
 * the frame lookup -- every frame the summary names in one read -- and as its
 * alt text when the frame cannot be read. The rest of the summary is sanitized
 * as every other summary surface is, and the `keyframe:` scheme is accepted
 * only as an image source.
 */
import React from 'react';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import { describe, it, expect, vi } from 'vitest';

import SummaryMarkdown, { frameIdsIn } from './SummaryMarkdown';

const frame = (id) => ({
  FrameId: id,
  ImageSignedUrl: `https://signed.example/${id}.jpg`,
  ThumbnailSignedUrl: `https://signed.example/${id}_t.jpg`,
});

const SLIDES = '**[00:30] Budget**\n\n![Slide at 00:30](keyframe:0000030000-ab12cd)\n\nBob covered Q3.';
const THREE_SLIDES =
  '![Slide at 00:30](keyframe:0000030000-ab12cd)\n\n![Slide at 01:00](keyframe:0000060000-ab12cd)\n\n' +
  '![Slide at 01:30](keyframe:0000090000-ab12cd)\n\n![Slide at 00:30 again](keyframe:0000030000-ab12cd)';

/** A batch loader answering the ids it is given, less those in `missing`. */
const framesLoader = (missing = []) =>
  vi.fn(async (_callId, ids) =>
    Object.fromEntries(ids.filter((id) => !missing.includes(id)).map((id) => [id, frame(id)])),
  );

describe('SummaryMarkdown', () => {
  it("shows a slide as the meeting's own frame, opening the full-size image", async () => {
    const loadFrames = framesLoader();
    const loadFrame = vi.fn();
    render(
      <SummaryMarkdown callId="m-1" loadFrames={loadFrames} loadFrame={loadFrame}>
        {SLIDES}
      </SummaryMarkdown>,
    );
    const img = await screen.findByRole('img', { name: 'Slide at 00:30' });
    expect(img.getAttribute('src')).toBe('https://signed.example/0000030000-ab12cd_t.jpg');
    expect(img.closest('a').getAttribute('href')).toBe('https://signed.example/0000030000-ab12cd.jpg');
    expect(img.closest('a').getAttribute('target')).toBe('_blank');
    expect(loadFrames).toHaveBeenCalledWith('m-1', ['0000030000-ab12cd']);
    expect(loadFrame).not.toHaveBeenCalled();
    expect(screen.getByText('Bob covered Q3.')).toBeTruthy();
  });

  it('reads every slide of the summary in one request, each frame once', async () => {
    const loadFrames = framesLoader();
    const loadFrame = vi.fn();
    render(
      <SummaryMarkdown callId="m-1" loadFrames={loadFrames} loadFrame={loadFrame}>
        {THREE_SLIDES}
      </SummaryMarkdown>,
    );
    expect(await screen.findAllByRole('img')).toHaveLength(4);
    expect(loadFrames).toHaveBeenCalledTimes(1);
    expect(loadFrames).toHaveBeenCalledWith('m-1', ['0000030000-ab12cd', '0000060000-ab12cd', '0000090000-ab12cd']);
    expect(loadFrame).not.toHaveBeenCalled();
  });

  it('finds the frames a summary refers to, each once, in order', () => {
    expect(frameIdsIn(THREE_SLIDES)).toEqual(['0000030000-ab12cd', '0000060000-ab12cd', '0000090000-ab12cd']);
    expect(frameIdsIn('no pictures here')).toEqual([]);
    expect(frameIdsIn(undefined)).toEqual([]);
  });

  it('reads only what is shown as a picture', () => {
    expect(frameIdsIn('[a link](keyframe:f-1) and keyframe:f-2 in text')).toEqual([]);
    expect(frameIdsIn('<img alt="x" src="keyframe:f-3"> ![y](keyframe:f-4)')).toEqual(['f-3', 'f-4']);
    // An id longer than a frame id is not cut down to one.
    expect(frameIdsIn(`![z](keyframe:${'a'.repeat(65)})`)).toEqual([]);
  });

  it('keeps the slides it shows while a changed summary reads the frames it now names', async () => {
    let answerSecond;
    const loadFrames = vi
      .fn()
      .mockImplementationOnce(async (_callId, ids) => Object.fromEntries(ids.map((id) => [id, frame(id)])))
      .mockImplementationOnce(
        (_callId, ids) =>
          new Promise((resolve) => {
            answerSecond = () => resolve(Object.fromEntries(ids.map((id) => [id, frame(id)])));
          }),
      );
    const { rerender } = render(
      <SummaryMarkdown callId="m-1" loadFrames={loadFrames} loadFrame={vi.fn()}>
        {SLIDES}
      </SummaryMarkdown>,
    );
    await screen.findByRole('img', { name: 'Slide at 00:30' });
    rerender(
      <SummaryMarkdown callId="m-1" loadFrames={loadFrames} loadFrame={vi.fn()}>
        {THREE_SLIDES}
      </SummaryMarkdown>,
    );
    await waitFor(() => expect(loadFrames).toHaveBeenCalledTimes(2));
    // Still shown while the second read is outstanding.
    expect(screen.getByRole('img', { name: 'Slide at 00:30' })).toBeTruthy();
    answerSecond();
    expect(await screen.findByRole('img', { name: 'Slide at 01:30' })).toBeTruthy();
  });

  it('shows the alt text for a frame that cannot be read', async () => {
    const loadFrames = framesLoader(['0000030000-ab12cd']);
    const { container } = render(
      <SummaryMarkdown callId="m-1" loadFrames={loadFrames} loadFrame={vi.fn()}>
        {SLIDES}
      </SummaryMarkdown>,
    );
    await waitFor(() => expect(loadFrames).toHaveBeenCalled());
    expect(screen.getByText('Slide at 00:30')).toBeTruthy();
    expect(container.querySelector('img')).toBeNull();
  });

  it('shows the alt text for every slide when the read fails', async () => {
    const loadFrames = vi.fn(async () => {
      throw new Error('Unauthorized');
    });
    const { container } = render(
      <SummaryMarkdown callId="m-1" loadFrames={loadFrames} loadFrame={vi.fn()}>
        {THREE_SLIDES}
      </SummaryMarkdown>,
    );
    await waitFor(() => expect(loadFrames).toHaveBeenCalled());
    expect(screen.getByText('Slide at 01:00')).toBeTruthy();
    expect(container.querySelector('img')).toBeNull();
  });

  it('reads the frame again by itself once when its link stops working, then shows the alt text', async () => {
    const loadFrames = framesLoader();
    const loadFrame = vi.fn(async (_callId, id) => frame(id));
    const { container } = render(
      <SummaryMarkdown callId="m-1" loadFrames={loadFrames} loadFrame={loadFrame}>
        {SLIDES}
      </SummaryMarkdown>,
    );
    fireEvent.error(await screen.findByRole('img'));
    await waitFor(() => expect(loadFrame).toHaveBeenCalledTimes(1));
    expect(loadFrame).toHaveBeenCalledWith('m-1', '0000030000-ab12cd');
    fireEvent.error(await screen.findByRole('img'));
    await waitFor(() => expect(container.querySelector('img')).toBeNull());
    expect(screen.getByText('Slide at 00:30')).toBeTruthy();
    expect(loadFrames).toHaveBeenCalledTimes(1);
  });

  it('accepts the keyframe scheme only as an image source, and still sanitizes the rest', async () => {
    const loadFrame = vi.fn();
    const { container } = render(
      <SummaryMarkdown callId="m-1" loadFrame={loadFrame} loadFrames={framesLoader()}>
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
    const loadFrames = vi.fn();
    render(
      <SummaryMarkdown callId="m-1" loadFrames={loadFrames}>
        ![chart](https://example.com/c.png)
      </SummaryMarkdown>,
    );
    expect(screen.getByRole('img', { name: 'chart' }).getAttribute('src')).toBe('https://example.com/c.png');
    // A summary with no slides reads no frames.
    expect(loadFrames).not.toHaveBeenCalled();
  });
});
