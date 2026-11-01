/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * What the key frame strip on the meeting page must do: list the frames in
 * time order, move the transcript (and, after the meeting, the recording) to a
 * selected frame, take new frames while the meeting runs, page through long
 * meetings, and reload its signed links when one has expired.
 */
import React from 'react';
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';

import VideoTimeline, { PAGE_SIZE } from './VideoTimeline';

const frame = (ms, extra = {}) => ({
  CallId: 'm-1',
  FrameId: `${String(ms).padStart(10, '0')}-ab12cd`,
  Timestamp: ms / 1000,
  VideoOffset: ms / 1000 + 10,
  ThumbnailSignedUrl: `https://signed.example/${ms}_thumb.jpg`,
  ImageSignedUrl: `https://signed.example/${ms}.jpg`,
  ...extra,
});

const noSubscribe = () => () => undefined;

describe('VideoTimeline', () => {
  it('renders nothing for a meeting without key frames', async () => {
    const loadFrames = vi.fn().mockResolvedValue({ VideoKeyframes: [], nextToken: null });
    const { container } = render(<VideoTimeline callId="m-1" loadFrames={loadFrames} subscribe={noSubscribe} />);
    await waitFor(() => expect(loadFrames).toHaveBeenCalledTimes(1));
    expect(container.querySelector('[data-testid="video-timeline"]')).toBeNull();
  });

  it('lists the frames in time order with their times', async () => {
    const loadFrames = vi.fn().mockResolvedValue({ VideoKeyframes: [frame(754000), frame(30000)], nextToken: null });
    render(<VideoTimeline callId="m-1" loadFrames={loadFrames} subscribe={noSubscribe} />);
    const images = await screen.findAllByRole('img');
    expect(images.map((i) => i.getAttribute('alt'))).toEqual(['Shared screen at 0:30', 'Shared screen at 12:34']);
    expect(loadFrames).toHaveBeenCalledWith({ callId: 'm-1', limit: PAGE_SIZE, nextToken: null, afterFrameId: null });
  });

  it('moves the transcript, and plays the recording when there is one, from a selected frame', async () => {
    const loadFrames = vi.fn().mockResolvedValue({ VideoKeyframes: [frame(30000)], nextToken: null });
    const onShowInTranscript = vi.fn();
    const onPlayFrom = vi.fn();
    const { rerender } = render(
      <VideoTimeline
        callId="m-1"
        loadFrames={loadFrames}
        subscribe={noSubscribe}
        onShowInTranscript={onShowInTranscript}
        onPlayFrom={onPlayFrom}
      />,
    );
    fireEvent.click(await screen.findByTitle('Key frame at 0:30'));
    expect(onShowInTranscript).toHaveBeenCalledWith(30);
    expect(onPlayFrom).not.toHaveBeenCalled();
    expect(screen.getByAltText('Shared screen at 0:30, full size').getAttribute('src')).toBe(
      'https://signed.example/30000.jpg',
    );

    rerender(
      <VideoTimeline
        callId="m-1"
        canPlayRecording
        loadFrames={loadFrames}
        subscribe={noSubscribe}
        onShowInTranscript={onShowInTranscript}
        onPlayFrom={onPlayFrom}
      />,
    );
    fireEvent.click(screen.getByTitle('Key frame at 0:30'));
    expect(onPlayFrom).toHaveBeenCalledWith(expect.objectContaining({ FrameId: '0000030000-ab12cd' }));
  });

  it('takes a new frame while the meeting runs, fetching only what came after the last one', async () => {
    let push;
    const subscribe = vi.fn((callId, onFrame) => {
      push = onFrame;
      return () => undefined;
    });
    const loadFrames = vi
      .fn()
      .mockResolvedValueOnce({ VideoKeyframes: [frame(30000)], nextToken: null })
      .mockResolvedValueOnce({ VideoKeyframes: [frame(90000)], nextToken: null });
    render(<VideoTimeline callId="m-1" isLive loadFrames={loadFrames} subscribe={subscribe} />);
    await screen.findByTitle('Key frame at 0:30');
    expect(subscribe).toHaveBeenCalledWith('m-1', expect.any(Function), expect.any(Function));
    push({ CallId: 'm-1', FrameId: '0000090000-ab12cd' });
    await screen.findByTitle('Key frame at 1:30');
    expect(loadFrames).toHaveBeenLastCalledWith(
      expect.objectContaining({ afterFrameId: '0000030000-ab12cd', nextToken: null }),
    );
  });

  it('fetches a frame announced while a page was still loading', async () => {
    let push;
    let release;
    const subscribe = (callId, onFrame) => {
      push = onFrame;
      return () => undefined;
    };
    const loadFrames = vi
      .fn()
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            release = () => resolve({ VideoKeyframes: [frame(30000)], nextToken: null });
          }),
      )
      .mockResolvedValueOnce({ VideoKeyframes: [frame(90000)], nextToken: null });
    render(<VideoTimeline callId="m-1" isLive loadFrames={loadFrames} subscribe={subscribe} />);
    await waitFor(() => expect(push).toBeDefined());
    push({ CallId: 'm-1', FrameId: '0000090000-ab12cd' });
    release();
    await screen.findByTitle('Key frame at 1:30');
    expect(loadFrames).toHaveBeenCalledTimes(2);
  });

  it('does not subscribe after the meeting', async () => {
    const subscribe = vi.fn(noSubscribe);
    const loadFrames = vi.fn().mockResolvedValue({ VideoKeyframes: [frame(30000)], nextToken: null });
    render(<VideoTimeline callId="m-1" loadFrames={loadFrames} subscribe={subscribe} />);
    await screen.findByTitle('Key frame at 0:30');
    expect(subscribe).not.toHaveBeenCalled();
  });

  it('pages through a long meeting', async () => {
    const loadFrames = vi
      .fn()
      .mockResolvedValueOnce({ VideoKeyframes: [frame(1000)], nextToken: 't1' })
      .mockResolvedValueOnce({ VideoKeyframes: [frame(2000)], nextToken: null });
    render(<VideoTimeline callId="m-1" loadFrames={loadFrames} subscribe={noSubscribe} />);
    fireEvent.click(await screen.findByText('Load more'));
    await screen.findByTitle('Key frame at 0:02');
    expect(loadFrames).toHaveBeenLastCalledWith(expect.objectContaining({ nextToken: 't1' }));
    expect(screen.queryByText('Load more')).toBeNull();
  });

  it('reloads the frames once when a signed link has expired', async () => {
    const loadFrames = vi
      .fn()
      .mockResolvedValueOnce({ VideoKeyframes: [frame(30000)], nextToken: null })
      .mockResolvedValueOnce({ VideoKeyframes: [frame(30000, { ThumbnailSignedUrl: 'https://signed.example/fresh' })] });
    render(<VideoTimeline callId="m-1" loadFrames={loadFrames} subscribe={noSubscribe} />);
    const image = await screen.findByAltText('Shared screen at 0:30');
    fireEvent.error(image);
    fireEvent.error(image); // a second failure inside the minute does not reload again
    await waitFor(() => expect(screen.getByAltText('Shared screen at 0:30').getAttribute('src')).toBe('https://signed.example/fresh'));
    expect(loadFrames).toHaveBeenCalledTimes(2);
  });

  it('says so when the frames cannot be loaded', async () => {
    const loadFrames = vi.fn().mockRejectedValue(new Error('Unauthorized'));
    render(<VideoTimeline callId="m-1" loadFrames={loadFrames} subscribe={noSubscribe} />);
    expect(await screen.findByText('The key frames could not be loaded.')).toBeTruthy();
  });
});
