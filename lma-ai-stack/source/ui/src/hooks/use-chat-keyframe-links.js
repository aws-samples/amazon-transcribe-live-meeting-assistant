/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Answers the meeting assistant chat's requests for frames of the shared screen
 * (STRANDS_RESOLVE_KEYFRAMES from public/strands-chat.html): signed links to the
 * frames an answer refers to, for the given meeting, through getVideoKeyframes
 * -- the same access check as the meeting page's strip. Used by every page that
 * hosts the chat. See docs/meeting-video.md.
 */
import { useEffect, useRef } from 'react';
import { generateClient } from 'aws-amplify/api';
import { ConsoleLogger } from 'aws-amplify/utils';

import getVideoKeyframes from '../graphql/queries/getVideoKeyframes';

const logger = new ConsoleLogger('useChatKeyframeLinks');
const FRAME_ID = /^[A-Za-z0-9_-]{1,64}$/;
const MAX_FRAMES = 10;

/** One frame of a meeting, with its signed links, or null; used by the chat and the summary. */
export const loadKeyframe = async (callId, frameId) => {
  const client = generateClient();
  const response = await client.graphql({ query: getVideoKeyframes, variables: { callId, frameId } });
  return response?.data?.getVideoKeyframes?.VideoKeyframes?.[0] || null;
};

/** The links for one request's frame ids, by id; frames that cannot be read are left out. */
export const resolveChatKeyframes = async (callId, frameIds, loadFrame = loadKeyframe) => {
  const ids = (Array.isArray(frameIds) ? frameIds : [])
    .filter((id) => typeof id === 'string' && FRAME_ID.test(id))
    .slice(0, MAX_FRAMES);
  const frames = {};
  await Promise.all(
    ids.map(async (frameId) => {
      try {
        const frame = await loadFrame(callId, frameId);
        if (frame) frames[frameId] = { image: frame.ImageSignedUrl, thumbnail: frame.ThumbnailSignedUrl };
      } catch (error) {
        logger.debug('could not resolve a key frame for the chat', error);
      }
    }),
  );
  return frames;
};

const useChatKeyframeLinks = (callId, loadFrame = loadKeyframe) => {
  // Read when a request arrives, so a page reused for another meeting answers
  // for the meeting it shows now.
  const callIdRef = useRef(callId);
  callIdRef.current = callId;

  useEffect(() => {
    const onMessage = async (event) => {
      if (event.origin !== window.location.origin) return;
      if (event.data?.type !== 'STRANDS_RESOLVE_KEYFRAMES') return;
      const frames = await resolveChatKeyframes(callIdRef.current, event.data.frameIds, loadFrame);
      event.source?.postMessage({ type: 'STRANDS_KEYFRAMES', requestId: event.data.requestId, frames }, event.origin);
    };
    window.addEventListener('message', onMessage);
    return () => window.removeEventListener('message', onMessage);
  }, [loadFrame]);
};

export default useChatKeyframeLinks;
