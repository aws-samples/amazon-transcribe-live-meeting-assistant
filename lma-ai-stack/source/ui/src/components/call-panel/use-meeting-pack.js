/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The meeting pack's files as the page builds them (meeting-pack.js): each
 * part's state -- building (with how far it has got), ready (its file and
 * size), unavailable (the reason) or failed -- kept while the meeting page is
 * open, so opening the dialog again, or the other dialog, reuses what was
 * built. A part is built again when what it is built from has changed since
 * (the summary's text, the number of key frames, a whiteboard's version), so
 * a meeting still going on gets a fresh snapshot.
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { ConsoleLogger } from 'aws-amplify/utils';

import { PART_SLIDES, PART_SUMMARY, PartUnavailable, buildPart, defaultBuilders } from './meeting-pack';

const logger = new ConsoleLogger('useMeetingPack');

/** What a part is built from, to tell whether a built file is still current. */
export const partSignature = (id, ctx) => {
  // The summary PDF lists the speakers' names; the slides are labelled by the chapters.
  if (id === PART_SUMMARY) return JSON.stringify([ctx.item?.callSummaryText ?? '', ctx.participants || []]);
  if (id === PART_SLIDES) return JSON.stringify([ctx.keyframeCount ?? 0, ctx.chapters || []]);
  return JSON.stringify((ctx.whiteboards || []).map((board) => [board.WhiteboardId, board.Version]));
};

const useMeetingPack = (ctx, { builders = defaultBuilders } = {}) => {
  const [parts, setParts] = useState({});
  const ctxRef = useRef(ctx);
  ctxRef.current = ctx;
  // Each part's latest build: its token (a later build supersedes it), what it
  // was built from, and the promise of its outcome.
  const runs = useRef({});
  const pending = useRef({});
  const counter = useRef(0);
  const callId = ctx.item?.callId;

  // Another meeting starts with nothing built, and stops the builds of this one.
  useEffect(() => {
    setParts({});
    runs.current = {};
    pending.current = {};
  }, [callId]);

  const update = useCallback((id, token, change) => {
    if (runs.current[id] !== token) return;
    setParts((previous) => ({ ...previous, [id]: { ...previous[id], ...change } }));
  }, []);

  /**
   * Starts building a part unless it is built and current, or being built.
   * Resolves to its state once built (or at once, when nothing was started).
   */
  const ensure = useCallback(
    async (id, { force = false } = {}) => {
      const { current } = ctxRef;
      const signature = partSignature(id, current);
      const held = pending.current[id];
      if (!force && held && held.signature === signature && held.status !== 'failed') return held.promise;
      counter.current += 1;
      const token = counter.current;
      runs.current[id] = token;
      const building = { status: 'building', signature, done: 0, total: 0 };
      setParts((previous) => ({ ...previous, [id]: building }));
      const run = async () => {
        try {
          const built = await buildPart(id, current, {
            builders,
            onProgress: (done, total) => update(id, token, { done, total }),
          });
          const ready = { status: 'ready', signature, ...built };
          update(id, token, ready);
          return ready;
        } catch (error) {
          const unavailable = error instanceof PartUnavailable;
          if (!unavailable) logger.error('could not build a part of the meeting pack', id, error);
          const failed = {
            status: unavailable ? 'unavailable' : 'failed',
            signature,
            message: unavailable ? error.message : 'This file could not be prepared.',
          };
          update(id, token, failed);
          if (pending.current[id]?.token === token) pending.current[id].status = failed.status;
          return failed;
        }
      };
      const entry = { signature, token, status: 'building', promise: run() };
      pending.current[id] = entry;
      return entry.promise;
    },
    [builders, update],
  );

  return { parts, ensure };
};

export default useMeetingPack;
