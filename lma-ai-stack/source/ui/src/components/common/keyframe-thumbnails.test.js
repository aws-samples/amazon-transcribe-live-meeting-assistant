/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The thumbnails a Word or PDF summary embeds: read through the meeting's key
 * frames, fetched through their signed links, scaled down, and left out when
 * they cannot be fetched.
 */
import { describe, it, expect, vi } from 'vitest';

import { fetchKeyframeThumbnails, MAX_EMBEDDED_KEYFRAMES } from './keyframe-thumbnails';

const frame = (id) => ({
  FrameId: id,
  ImageSignedUrl: `https://bucket.example/${id}.jpg?sig=1`,
  ThumbnailSignedUrl: `https://bucket.example/${id}_thumb.jpg?sig=1`,
});
const framesById = (ids) => Object.fromEntries(ids.map((id) => [id, frame(id)]));
const resized = (url) => ({ data: new Uint8Array([1, 2, 3]), width: 480, height: 270, from: url });

describe('fetchKeyframeThumbnails', () => {
  it('reads the frames in one request and fetches each full-size picture, scaled down', async () => {
    const loadFrames = vi.fn(async (_callId, ids) => framesById(ids));
    const fetchImage = vi.fn(async (url) => url);
    const resize = vi.fn(async (blob) => resized(blob));
    const thumbnails = await fetchKeyframeThumbnails('Weekly sync', ['0001-a', '0002-b', '0001-a'], {
      loadFrames,
      fetchImage,
      resize,
    });
    expect(loadFrames).toHaveBeenCalledTimes(1);
    expect(loadFrames).toHaveBeenCalledWith('Weekly sync', ['0001-a', '0002-b']);
    expect(Object.keys(thumbnails).sort()).toEqual(['0001-a', '0002-b']);
    expect(thumbnails['0001-a'].from).toBe('https://bucket.example/0001-a.jpg?sig=1');
    expect(thumbnails['0001-a'].width).toBe(480);
  });

  it("falls back to the frame's thumbnail link when the full-size one cannot be fetched", async () => {
    const fetchImage = vi.fn(async (url) => {
      if (!url.includes('_thumb')) throw new Error('403');
      return url;
    });
    const thumbnails = await fetchKeyframeThumbnails('m', ['0001-a'], {
      loadFrames: async (_c, ids) => framesById(ids),
      fetchImage,
      resize: async (blob) => resized(blob),
    });
    expect(thumbnails['0001-a'].from).toBe('https://bucket.example/0001-a_thumb.jpg?sig=1');
  });

  it('leaves out a frame that cannot be fetched or decoded, and keeps the others', async () => {
    const thumbnails = await fetchKeyframeThumbnails('m', ['0001-a', '0002-b', '0003-c'], {
      // 0003-c is not in the answer: deleted with the meeting's retention, say.
      loadFrames: async () => framesById(['0001-a', '0002-b']),
      fetchImage: async (url) => {
        if (url.includes('0002-b')) throw new TypeError('Failed to fetch');
        return url;
      },
      resize: async (blob) => resized(blob),
    });
    expect(Object.keys(thumbnails)).toEqual(['0001-a']);
  });

  it('answers with no thumbnails when the frames cannot be read at all', async () => {
    const fetchImage = vi.fn();
    const thumbnails = await fetchKeyframeThumbnails('m', ['0001-a'], {
      loadFrames: async () => {
        throw new Error('Unauthorized');
      },
      fetchImage,
    });
    expect(thumbnails).toEqual({});
    expect(fetchImage).not.toHaveBeenCalled();
  });

  it(`reads at most ${MAX_EMBEDDED_KEYFRAMES} frames by default, the first ones named`, async () => {
    const ids = Array.from({ length: MAX_EMBEDDED_KEYFRAMES + 5 }, (_, i) => `${String(i).padStart(4, '0')}-f`);
    const loadFrames = vi.fn(async (_c, wanted) => framesById(wanted));
    const thumbnails = await fetchKeyframeThumbnails('m', ids, {
      loadFrames,
      fetchImage: async (url) => url,
      resize: async (blob) => resized(blob),
    });
    expect(loadFrames.mock.calls[0][1]).toEqual(ids.slice(0, MAX_EMBEDDED_KEYFRAMES));
    expect(Object.keys(thumbnails)).toHaveLength(MAX_EMBEDDED_KEYFRAMES);
    expect(thumbnails[ids[MAX_EMBEDDED_KEYFRAMES]]).toBeUndefined();
  });

  it('ignores ids that are not frame ids, and reads nothing without a meeting', async () => {
    const loadFrames = vi.fn(async () => ({}));
    expect(await fetchKeyframeThumbnails('m', ['bad id/..'], { loadFrames })).toEqual({});
    expect(await fetchKeyframeThumbnails('', ['0001-a'], { loadFrames })).toEqual({});
    expect(loadFrames).not.toHaveBeenCalled();
  });
});
