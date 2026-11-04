/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Whether the meeting page's assistant panel is open.
 *
 * The choice is remembered per signed-in user in this browser's localStorage,
 * and applies to every meeting. A live meeting this user has not looked at
 * before opens with the panel open whatever the remembered choice, because a
 * running meeting is where the assistant is most used; from then on that
 * meeting follows the remembered choice like any other. A host that asks for
 * the panel explicitly (the embeddable components, through `defaultOpen`)
 * gets it, and its toggles are not remembered, so an embed does not change
 * the user's choice in the application.
 *
 * `mounted` turns true the first time the panel opens and stays true: the
 * chat frame is created then, not before, and is hidden rather than removed
 * when the panel closes, so its conversation is kept.
 */
import { useCallback, useEffect, useState } from 'react';

export const OPEN_STORAGE_KEY = 'lma-meeting-assistant-open';
export const SEEN_LIVE_STORAGE_KEY = 'lma-meeting-assistant-seen-live';
/** How many live meetings are remembered as seen; older ones are forgotten first. */
export const MAX_SEEN_LIVE = 100;

const keyFor = (prefix, username) => `${prefix}:${username || ''}`;

const read = (storage, key) => {
  try {
    return storage?.getItem(key) ?? null;
  } catch {
    return null;
  }
};

const write = (storage, key, value) => {
  try {
    storage?.setItem(key, value);
  } catch {
    // Storage full or unavailable (a private window, an embed with storage
    // blocked): the choice lasts for this page only.
  }
};

const seenLiveMeetings = (storage, username) => {
  try {
    const parsed = JSON.parse(read(storage, keyFor(SEEN_LIVE_STORAGE_KEY, username)) || '[]');
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
};

/** The remembered choice: true, false, or null when the user has never made one. */
export const storedAssistantOpen = (username, storage = window.localStorage) => {
  const value = read(storage, keyFor(OPEN_STORAGE_KEY, username));
  if (value === 'true') return true;
  if (value === 'false') return false;
  return null;
};

export const storeAssistantOpen = (username, open, storage = window.localStorage) =>
  write(storage, keyFor(OPEN_STORAGE_KEY, username), open ? 'true' : 'false');

/** Records that this user has seen a live meeting, so it opens with the panel open only the first time. */
export const rememberLiveMeeting = (username, callId, storage = window.localStorage) => {
  if (!callId) return;
  const seen = seenLiveMeetings(storage, username).filter((id) => id !== callId);
  seen.push(callId);
  write(storage, keyFor(SEEN_LIVE_STORAGE_KEY, username), JSON.stringify(seen.slice(-MAX_SEEN_LIVE)));
};

/**
 * Whether the panel starts open for a meeting: open for a live meeting not
 * seen before, otherwise the remembered choice, and closed when there is none.
 */
export const initialAssistantOpen = ({ callId, isLive, username, storage = window.localStorage }) => {
  if (isLive && callId && !seenLiveMeetings(storage, username).includes(callId)) return true;
  return storedAssistantOpen(username, storage) === true;
};

const useAssistantPanel = ({ enabled, callId, isLive, username, defaultOpen = undefined }) => {
  const remembers = defaultOpen === undefined;
  const decide = () => {
    if (!enabled) return false;
    if (!remembers) return Boolean(defaultOpen);
    return initialAssistantOpen({ callId, isLive, username });
  };

  const [state, setState] = useState(() => {
    const open = decide();
    return { callId, open, mounted: open };
  });

  // Another meeting shown in the same panel (the meeting list's split panel):
  // decided again for that meeting. The frame, once created, is kept.
  let current = state;
  if (state.callId !== callId) {
    const open = decide();
    current = { callId, open, mounted: state.mounted || open };
    setState(current);
  }

  useEffect(() => {
    if (enabled && remembers && isLive) rememberLiveMeeting(username, callId);
  }, [enabled, remembers, isLive, username, callId]);

  // `remember: false` for a closing the viewer did not ask for as such (the
  // panel making way for the transcript where it lies over the page).
  const setOpen = useCallback(
    (open, { remember = true } = {}) => {
      setState((previous) => ({ ...previous, open, mounted: previous.mounted || open }));
      if (remembers && remember) storeAssistantOpen(username, open);
    },
    [remembers, username],
  );

  return { open: enabled && current.open, mounted: enabled && current.mounted, setOpen };
};

export default useAssistantPanel;
