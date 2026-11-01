/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The chat's frame requests are answered for the meeting the page shows, only
 * for well-formed ids, only from the page's own origin, and frames that cannot
 * be read are left out.
 */
import { renderHook, waitFor } from '@testing-library/react';
import { describe, it, expect, vi } from 'vitest';

import useChatKeyframeLinks, { resolveChatKeyframes } from './use-chat-keyframe-links';

const frame = (id) => ({
  FrameId: id,
  ImageSignedUrl: `https://signed/${id}.jpg`,
  ThumbnailSignedUrl: `https://signed/${id}_t.jpg`,
});

describe('resolveChatKeyframes', () => {
  it('signs the well-formed ids of the meeting, at most ten, and leaves out the ones it cannot read', async () => {
    const loadFrame = vi.fn(async (callId, id) => {
      if (id === 'gone') return null;
      if (id === 'boom') throw new Error('Unauthorized');
      return frame(id);
    });
    const ids = ['a-1', 'gone', 'boom', 'bad id', 'f#x', ...Array.from({ length: 12 }, (_, i) => `n-${i}`)];
    const frames = await resolveChatKeyframes('m-1', ids, loadFrame);
    expect(frames['a-1']).toEqual({ image: 'https://signed/a-1.jpg', thumbnail: 'https://signed/a-1_t.jpg' });
    expect(Object.keys(frames)).not.toContain('gone');
    expect(loadFrame).toHaveBeenCalledTimes(10);
    expect(loadFrame.mock.calls.every(([callId]) => callId === 'm-1')).toBe(true);
    expect(loadFrame.mock.calls.map(([, id]) => id)).not.toContain('bad id');
  });
});

describe('useChatKeyframeLinks', () => {
  const ask = (source, origin = window.location.origin) =>
    window.dispatchEvent(
      new MessageEvent('message', {
        origin,
        source,
        data: { type: 'STRANDS_RESOLVE_KEYFRAMES', requestId: 'r-1', frameIds: ['a-1'] },
      }),
    );

  it('answers the chat for the meeting the page shows now', async () => {
    const loadFrame = vi.fn(async (_callId, id) => frame(id));
    const source = { postMessage: vi.fn() };
    const { rerender } = renderHook(({ callId }) => useChatKeyframeLinks(callId, loadFrame), {
      initialProps: { callId: 'm-1' },
    });
    rerender({ callId: 'm-2' });
    ask(source);
    await waitFor(() => expect(source.postMessage).toHaveBeenCalled());
    expect(loadFrame).toHaveBeenCalledWith('m-2', 'a-1');
    expect(source.postMessage.mock.calls[0][0]).toEqual({
      type: 'STRANDS_KEYFRAMES',
      requestId: 'r-1',
      frames: { 'a-1': { image: 'https://signed/a-1.jpg', thumbnail: 'https://signed/a-1_t.jpg' } },
    });
  });

  it('ignores a request from another origin', async () => {
    const loadFrame = vi.fn();
    const source = { postMessage: vi.fn() };
    renderHook(() => useChatKeyframeLinks('m-1', loadFrame));
    ask(source, 'https://elsewhere.example');
    await new Promise((r) => {
      setTimeout(r, 10);
    });
    expect(loadFrame).not.toHaveBeenCalled();
    expect(source.postMessage).not.toHaveBeenCalled();
  });
});
