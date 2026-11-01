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
      .mockResolvedValueOnce({
        VideoKeyframes: [frame(30000, { ThumbnailSignedUrl: 'https://signed.example/fresh' })],
      });
    render(<VideoTimeline callId="m-1" loadFrames={loadFrames} subscribe={noSubscribe} />);
    const image = await screen.findByAltText('Shared screen at 0:30');
    fireEvent.error(image);
    fireEvent.error(image); // a second failure inside the minute does not reload again
    await waitFor(() =>
      expect(screen.getByAltText('Shared screen at 0:30').getAttribute('src')).toBe('https://signed.example/fresh'),
    );
    expect(loadFrames).toHaveBeenCalledTimes(2);
  });

  it('keeps the frames already paged through when it renews expired links', async () => {
    const loadFrames = vi
      .fn()
      .mockResolvedValueOnce({ VideoKeyframes: [frame(1000)], nextToken: 't1' })
      .mockResolvedValueOnce({ VideoKeyframes: [frame(2000)], nextToken: 't2' })
      // The renewal walks the same two pages again.
      .mockResolvedValueOnce({
        VideoKeyframes: [frame(1000, { ThumbnailSignedUrl: 'https://signed.example/f1' })],
        nextToken: 't1',
      })
      .mockResolvedValueOnce({
        VideoKeyframes: [frame(2000, { ThumbnailSignedUrl: 'https://signed.example/f2' })],
        nextToken: 't2',
      });
    render(<VideoTimeline callId="m-1" loadFrames={loadFrames} subscribe={noSubscribe} />);
    fireEvent.click(await screen.findByText('Load more'));
    fireEvent.click(await screen.findByTitle('Key frame at 0:02'));
    fireEvent.error(screen.getByAltText('Shared screen at 0:02'));
    await waitFor(() =>
      expect(screen.getByAltText('Shared screen at 0:02').getAttribute('src')).toBe('https://signed.example/f2'),
    );
    expect(screen.getByAltText('Shared screen at 0:01').getAttribute('src')).toBe('https://signed.example/f1');
    expect(loadFrames).toHaveBeenCalledTimes(4);
    expect(loadFrames.mock.calls[3][0]).toEqual(expect.objectContaining({ nextToken: 't1' }));
    // Still selected, and the cursor is where it was.
    expect(screen.getByAltText('Shared screen at 0:02, full size')).toBeTruthy();
    expect(screen.getByText('Load more')).toBeTruthy();
  });

  it("does not show one meeting's frames under another after a quick switch", async () => {
    let releaseFirst;
    const loadFrames = vi.fn(({ callId }) => {
      if (callId === 'm-1') {
        return new Promise((resolve) => {
          releaseFirst = () => resolve({ VideoKeyframes: [frame(1000, { CallId: 'm-1' })], nextToken: null });
        });
      }
      return Promise.resolve({ VideoKeyframes: [frame(5000, { CallId: 'm-2' })], nextToken: null });
    });
    const { rerender } = render(<VideoTimeline callId="m-1" loadFrames={loadFrames} subscribe={noSubscribe} />);
    await waitFor(() => expect(releaseFirst).toBeDefined());
    rerender(<VideoTimeline callId="m-2" loadFrames={loadFrames} subscribe={noSubscribe} />);
    await screen.findByTitle('Key frame at 0:05');
    releaseFirst();
    await new Promise((r) => {
      setTimeout(r, 20);
    });
    expect(screen.queryByTitle('Key frame at 0:01')).toBeNull();
  });

  it('ends the paging at an empty page', async () => {
    const loadFrames = vi
      .fn()
      .mockResolvedValueOnce({ VideoKeyframes: [frame(1000)], nextToken: 't1' })
      .mockResolvedValueOnce({ VideoKeyframes: [], nextToken: 't2' });
    render(<VideoTimeline callId="m-1" loadFrames={loadFrames} subscribe={noSubscribe} />);
    fireEvent.click(await screen.findByText('Load more'));
    await waitFor(() => expect(screen.queryByText('Load more')).toBeNull());
    expect(screen.getByText('(1)')).toBeTruthy();
  });

  it('asks for the player to be shown only for an explicit play', async () => {
    const onPlayFrom = vi.fn();
    const loadFrames = vi.fn().mockResolvedValue({ VideoKeyframes: [frame(30000)], nextToken: null });
    render(
      <VideoTimeline
        callId="m-1"
        canPlayRecording
        loadFrames={loadFrames}
        subscribe={noSubscribe}
        onPlayFrom={onPlayFrom}
      />,
    );
    fireEvent.click(await screen.findByTitle('Key frame at 0:30'));
    expect(onPlayFrom).toHaveBeenLastCalledWith(expect.objectContaining({ FrameId: '0000030000-ab12cd' }));
    expect(onPlayFrom.mock.calls[0]).toHaveLength(1);
    fireEvent.click(screen.getByText('Play recording from here'));
    expect(onPlayFrom).toHaveBeenLastCalledWith(expect.objectContaining({ FrameId: '0000030000-ab12cd' }), {
      reveal: true,
    });
  });

  it('finds frames by the text on them, across pages not loaded yet', async () => {
    const loadFrames = vi
      .fn()
      .mockResolvedValueOnce({ VideoKeyframes: [frame(1000, { SlideText: 'Roadmap for Q3' })], nextToken: 't1' })
      .mockResolvedValueOnce({ VideoKeyframes: [frame(2000, { SlideText: 'Hiring plan' })], nextToken: null });
    const { container } = render(<VideoTimeline callId="m-1" loadFrames={loadFrames} subscribe={noSubscribe} />);
    await screen.findByTitle('Key frame at 0:01');
    const input = container.querySelector('input');
    fireEvent.change(input, { target: { value: 'hiring' } });
    await screen.findByTitle('Key frame at 0:02');
    expect(loadFrames).toHaveBeenCalledTimes(2);
    expect(screen.queryByTitle('Key frame at 0:01')).toBeNull();
    expect(screen.getByText('Hiring plan')).toBeTruthy();
    fireEvent.change(input, { target: { value: 'budget' } });
    expect(await screen.findByText('No frame shows that text.')).toBeTruthy();
  });

  it('says so when the frames cannot be loaded', async () => {
    const loadFrames = vi.fn().mockRejectedValue(new Error('Unauthorized'));
    render(<VideoTimeline callId="m-1" loadFrames={loadFrames} subscribe={noSubscribe} />);
    expect(await screen.findByText('The key frames could not be loaded.')).toBeTruthy();
  });
  it('stops paging a search at a page that fails, and offers to try again', async () => {
    const loadFrames = vi
      .fn()
      .mockResolvedValueOnce({ VideoKeyframes: [frame(1000, { SlideText: 'Roadmap' })], nextToken: 't1' })
      .mockRejectedValueOnce(new Error('Throttled'))
      .mockResolvedValue({ VideoKeyframes: [frame(2000, { SlideText: 'Hiring plan' })], nextToken: null });
    const { container } = render(<VideoTimeline callId="m-1" loadFrames={loadFrames} subscribe={noSubscribe} />);
    await screen.findByTitle('Key frame at 0:01');
    fireEvent.change(container.querySelector('input'), { target: { value: 'hiring' } });
    expect(await screen.findByText('The key frames could not be loaded.')).toBeTruthy();
    await new Promise((r) => {
      setTimeout(r, 50);
    });
    expect(loadFrames).toHaveBeenCalledTimes(2);
    fireEvent.click(screen.getByText('Load more'));
    expect(await screen.findByText('Hiring plan')).toBeTruthy();
  });

  it('reads the shown frames again when a search starts and one has no text yet', async () => {
    const loadFrames = vi
      .fn()
      .mockResolvedValueOnce({ VideoKeyframes: [frame(1000)], nextToken: null })
      .mockResolvedValueOnce({ VideoKeyframes: [frame(1000, { SlideText: 'Hiring plan' })], nextToken: null });
    const { container } = render(<VideoTimeline callId="m-1" loadFrames={loadFrames} subscribe={noSubscribe} />);
    await screen.findByTitle('Key frame at 0:01');
    const input = container.querySelector('input');
    fireEvent.change(input, { target: { value: 'hiring' } });
    expect(await screen.findByText('Hiring plan')).toBeTruthy();
    fireEvent.change(input, { target: { value: 'hiring p' } });
    await new Promise((r) => {
      setTimeout(r, 50);
    });
    expect(loadFrames).toHaveBeenCalledTimes(2);
  });
});
