/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Answers the meeting assistant chat's requests for frames of the shared screen
 * (STRANDS_RESOLVE_KEYFRAMES from public/strands-chat.html): signed links to the
 * frames an answer refers to, for the given meeting, through getVideoKeyframes
 * -- the same access check as the meeting page's strip -- in one request for
 * all of them. Used by every page that hosts the chat. See docs/meeting-video.md.
 */
import { useEffect, useRef } from 'react';
import { generateClient } from 'aws-amplify/api';
import { ConsoleLogger } from 'aws-amplify/utils';

import getVideoKeyframes from '../graphql/queries/getVideoKeyframes';

const logger = new ConsoleLogger('useChatKeyframeLinks');
export const FRAME_ID = /^[A-Za-z0-9_-]{1,64}$/;
const MAX_FRAMES = 10;
/** The most frames getVideoKeyframes reads by id in one request. */
export const FRAMES_PER_REQUEST = 100;

/**
 * One frame of a meeting, with its signed links, or null. Used by a summary
 * picture read on its own: outside a summary, in a form the summary's read did
 * not find, or again after its link has lapsed.
 */
export const loadKeyframe = async (callId, frameId) => {
  const client = generateClient();
  const response = await client.graphql({ query: getVideoKeyframes, variables: { callId, frameId } });
  return response?.data?.getVideoKeyframes?.VideoKeyframes?.[0] || null;
};

/**
 * Several frames of a meeting, with their signed links, by id: one request per
 * hundred frames rather than one per frame. A frame that does not exist is not
 * in the answer; a caller without access to the meeting gets an error for the
 * whole request. Used by the chat and the summary.
 */
export const loadKeyframes = async (callId, frameIds) => {
  const ids = [...new Set(frameIds)];
  const client = generateClient();
  const frames = {};
  for (let start = 0; start < ids.length; start += FRAMES_PER_REQUEST) {
    // eslint-disable-next-line no-await-in-loop -- a summary rarely needs a second request
    const response = await client.graphql({
      query: getVideoKeyframes,
      variables: { callId, frameIds: ids.slice(start, start + FRAMES_PER_REQUEST) },
    });
    (response?.data?.getVideoKeyframes?.VideoKeyframes || []).forEach((frame) => {
      if (frame?.FrameId) frames[frame.FrameId] = frame;
    });
  }
  return frames;
};

/** The links for one request's frame ids, by id; frames that cannot be read are left out. */
export const resolveChatKeyframes = async (callId, frameIds, loadFrames = loadKeyframes) => {
  const ids = (Array.isArray(frameIds) ? frameIds : [])
    .filter((id) => typeof id === 'string' && FRAME_ID.test(id))
    .slice(0, MAX_FRAMES);
  if (ids.length === 0) return {};
  let loaded = {};
  try {
    loaded = (await loadFrames(callId, ids)) || {};
  } catch (error) {
    logger.debug('could not resolve key frames for the chat', error);
  }
  const frames = {};
  ids.forEach((frameId) => {
    const frame = loaded[frameId];
    if (frame) frames[frameId] = { image: frame.ImageSignedUrl, thumbnail: frame.ThumbnailSignedUrl };
  });
  return frames;
};

const useChatKeyframeLinks = (callId, loadFrames = loadKeyframes) => {
  // Read when a request arrives, so a page reused for another meeting answers
  // for the meeting it shows now.
  const callIdRef = useRef(callId);
  callIdRef.current = callId;

  useEffect(() => {
    const onMessage = async (event) => {
      if (event.origin !== window.location.origin) return;
      if (event.data?.type !== 'STRANDS_RESOLVE_KEYFRAMES') return;
      const frames = await resolveChatKeyframes(callIdRef.current, event.data.frameIds, loadFrames);
      event.source?.postMessage({ type: 'STRANDS_KEYFRAMES', requestId: event.data.requestId, frames }, event.origin);
    };
    window.addEventListener('message', onMessage);
    return () => window.removeEventListener('message', onMessage);
  }, [loadFrames]);
};

export default useChatKeyframeLinks;
