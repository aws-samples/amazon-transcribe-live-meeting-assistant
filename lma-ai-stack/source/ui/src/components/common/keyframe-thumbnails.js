/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Thumbnails of the key frames a summary names, for the downloads that can hold
 * a picture (Word and PDF). Each frame is read through getVideoKeyframes, the
 * same request and access check as the meeting page's Summary tab, and its
 * picture is fetched in the browser through the signed link that request
 * returns. The picture is scaled down to THUMBNAIL_WIDTH pixels and re-encoded
 * as a JPEG, to keep the file small. A frame that cannot be read or fetched is
 * left out of the answer, and the download writes its text description instead.
 * See docs/meeting-video.md.
 */
import { ConsoleLogger } from 'aws-amplify/utils';

import { FRAME_ID, loadKeyframes } from '../../hooks/use-chat-keyframe-links';

const logger = new ConsoleLogger('KeyframeThumbnails');

/** The most key frames one download embeds; later ones are written as their text description. */
export const MAX_EMBEDDED_KEYFRAMES = 40;
/** The width, in pixels, a key frame is scaled down to (never up). */
export const THUMBNAIL_WIDTH = 480;
const JPEG_QUALITY = 0.8;
// How many pictures are fetched at once.
const CONCURRENT_FETCHES = 4;

/**
 * The picture at a signed link, as a Blob. `cache: 'no-store'`, because the
 * browser may hold the same picture from the Summary tab's <img>, fetched
 * without CORS, and a cached answer of that kind cannot be read by a script.
 */
export const fetchImageBlob = async (url) => {
  const response = await fetch(url, { mode: 'cors', credentials: 'omit', cache: 'no-store' });
  if (!response.ok) throw new Error(`key frame fetch answered ${response.status}`);
  return response.blob();
};

/**
 * A picture (a Blob) as a JPEG no wider than `width`, on a white background:
 * { data: Uint8Array, width, height }, the size in pixels.
 */
export const downscaleToJpeg = async (blob, width = THUMBNAIL_WIDTH) => {
  const bitmap = await createImageBitmap(blob);
  try {
    const scale = Math.min(1, width / bitmap.width);
    const w = Math.max(1, Math.round(bitmap.width * scale));
    const h = Math.max(1, Math.round(bitmap.height * scale));
    const canvas = document.createElement('canvas');
    canvas.width = w;
    canvas.height = h;
    const context = canvas.getContext('2d');
    context.fillStyle = '#ffffff';
    context.fillRect(0, 0, w, h);
    context.drawImage(bitmap, 0, 0, w, h);
    const jpeg = await new Promise((resolve, reject) => {
      canvas.toBlob(
        (out) => (out ? resolve(out) : reject(new Error('the key frame could not be encoded'))),
        'image/jpeg',
        JPEG_QUALITY,
      );
    });
    return { data: new Uint8Array(await jpeg.arrayBuffer()), width: w, height: h };
  } finally {
    bitmap.close?.();
  }
};

// Runs `work` over `items`, at most `limit` at a time.
const forEachLimited = async (items, limit, work) => {
  let next = 0;
  const worker = async () => {
    while (next < items.length) {
      const item = items[next];
      next += 1;
      // eslint-disable-next-line no-await-in-loop -- each worker takes one item at a time
      await work(item);
    }
  };
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, worker));
};

/**
 * Thumbnails of a meeting's key frames, by FrameId: { [frameId]: { data,
 * width, height } }. Only the first `max` distinct ids are read. A frame is
 * fetched from its full-size link, else from its thumbnail link; a frame
 * neither can be fetched from is not in the answer, nor is any frame when the
 * frames cannot be read at all. Never rejects.
 */
export const fetchKeyframeThumbnails = async (
  callId,
  frameIds,
  {
    loadFrames = loadKeyframes,
    fetchImage = fetchImageBlob,
    resize = downscaleToJpeg,
    max = MAX_EMBEDDED_KEYFRAMES,
  } = {},
) => {
  const ids = [...new Set(frameIds || [])].filter((id) => typeof id === 'string' && FRAME_ID.test(id)).slice(0, max);
  if (!callId || !ids.length) return {};
  let frames;
  try {
    frames = (await loadFrames(callId, ids)) || {};
  } catch (error) {
    logger.debug('could not read the key frames for the download', error);
    return {};
  }
  const thumbnails = {};
  await forEachLimited(ids, CONCURRENT_FETCHES, async (id) => {
    const frame = frames[id];
    const links = [frame?.ImageSignedUrl, frame?.ThumbnailSignedUrl].filter(Boolean);
    for (let i = 0; i < links.length; i += 1) {
      try {
        // eslint-disable-next-line no-await-in-loop -- the thumbnail link is tried only when the full-size one fails
        thumbnails[id] = await resize(await fetchImage(links[i]));
        return;
      } catch (error) {
        logger.debug('could not fetch a key frame for the download', id, error);
      }
    }
  });
  return thumbnails;
};
