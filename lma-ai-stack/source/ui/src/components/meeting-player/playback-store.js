/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Where the meeting page's player is, for the parts of the page that follow
 * it (the transcript's highlight).
 *
 * A small store rather than React state on the page: a playing recording
 * reports its position several times a second, and holding that in the
 * page's state would re-render the whole meeting page each time. Readers
 * subscribe with usePlaybackSelector and re-render only when the value they
 * select changes -- for the transcript, the id of the segment being heard.
 *
 * State: `seconds` (transcript clock), `playing`, `engaged`, which is false
 * until the recording has been played or moved, so a transcript opened with
 * the player at 0:00 highlights nothing, and `seekCount`, which counts the
 * explicit moves of the recording (a reader who scrolled away is followed
 * again after one).
 */
import { useCallback, useState, useSyncExternalStore } from 'react';

export const INITIAL_PLAYBACK = Object.freeze({ seconds: 0, playing: false, engaged: false, seekCount: 0 });

export const createPlaybackStore = () => {
  let state = INITIAL_PLAYBACK;
  const listeners = new Set();
  return {
    get: () => state,
    set: (patch) => {
      const next = { ...state, ...patch };
      if (Object.keys(next).every((key) => next[key] === state[key])) return;
      state = next;
      listeners.forEach((listener) => listener());
    },
    reset: () => {
      state = INITIAL_PLAYBACK;
      listeners.forEach((listener) => listener());
    },
    subscribe: (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
  };
};

/** One store for the lifetime of the component that calls it. */
export const usePlaybackStore = () => {
  const [store] = useState(createPlaybackStore);
  return store;
};

const NO_STORE = { get: () => INITIAL_PLAYBACK, subscribe: () => () => undefined };

/**
 * A value derived from the playback state, re-rendering only when it changes.
 * `selector` must return a primitive (or a stable value) for that to hold.
 */
export const usePlaybackSelector = (store, selector) => {
  const source = store || NO_STORE;
  const getSnapshot = useCallback(() => selector(source.get()), [source, selector]);
  return useSyncExternalStore(source.subscribe, getSnapshot, getSnapshot);
};
