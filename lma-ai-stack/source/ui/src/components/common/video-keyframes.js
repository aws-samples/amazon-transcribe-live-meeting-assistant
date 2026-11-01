/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Helpers for a meeting's key frames (docs/meeting-video.md): the pictures of
 * the shared screen kept each time it changed and held still. Kept free of
 * React so they can be tested directly.
 */

/**
 * The frames of `existing` and `incoming` in time order, one per FrameId. A
 * frame in `incoming` replaces the stored one: it carries fresher signed links.
 * FrameId starts with the zero-padded time in milliseconds, so it sorts in
 * time order.
 */
export const mergeKeyframes = (existing, incoming) => {
  const byId = new Map();
  [...(existing || []), ...(incoming || [])].forEach((frame) => {
    if (frame?.FrameId) byId.set(frame.FrameId, frame);
  });
  // Code-unit order, not locale order, which may skip the hyphen.
  return [...byId.values()].sort((a, b) => {
    if (a.FrameId === b.FrameId) return 0;
    return a.FrameId < b.FrameId ? -1 : 1;
  });
};

/** "m:ss" under an hour, "h:mm:ss" from an hour, for a time on the transcript's clock. */
export const formatFrameTime = (seconds) => {
  const total = Math.max(0, Math.floor(Number(seconds) || 0));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = String(total % 60).padStart(2, '0');
  return h ? `${h}:${String(m).padStart(2, '0')}:${s}` : `${m}:${s}`;
};

/**
 * Where to start the recording for a frame: its position in the recording when
 * known, and otherwise its transcript time, which is the recording's clock for
 * every source that records one.
 */
export const recordingSecondsFor = (frame) => {
  const offset = Number(frame?.VideoOffset);
  if (frame?.VideoOffset !== null && frame?.VideoOffset !== undefined && Number.isFinite(offset)) return offset;
  const t = Number(frame?.Timestamp);
  return Number.isFinite(t) ? t : 0;
};

/**
 * Of a list of transcript start times (in page order), the index of the
 * segment to show for time `seconds`: the last one that started at or before
 * it, or the first when it is earlier than all of them. -1 for an empty list.
 */
export const segmentIndexForTime = (startTimes, seconds) => {
  if (!startTimes?.length) return -1;
  let best = -1;
  let bestStart = -Infinity;
  startTimes.forEach((start, i) => {
    if (start <= seconds && start >= bestStart) {
      best = i;
      bestStart = start;
    }
  });
  if (best !== -1) return best;
  let first = 0;
  startTimes.forEach((start, i) => {
    if (start < startTimes[first]) first = i;
  });
  return first;
};
