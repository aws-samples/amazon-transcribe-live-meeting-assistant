/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Links between a meeting page's surfaces that live in different parts of the
 * page: the Notifications drawer (the app layout's), and the assistant panel
 * and Whiteboards tab (the meeting page's own, CallPanel).
 *
 * The meeting page registers what it can do for its meeting (useMeetingActions);
 * a guidance card or a whiteboard panel asks for it (requestMeetingAction) and
 * shows its button only while some page can (useMeetingActionAvailable). The
 * actions:
 *
 * - ASK_ASSISTANT `{ label, text, question }`: open the assistant chat with
 *   `text` attached as quoted context, labelled `label`, and `question` as the
 *   question sent when the user sends nothing of their own;
 * - ADD_TO_WHITEBOARD `{ title, text }`: offer to add `text` to a whiteboard,
 *   its panel titled `title` when it is a new one;
 * - OPEN_WHITEBOARD `{ whiteboardId, panelId }`: show that whiteboard.
 *
 * Nothing leaves the page: every action is carried out by the meeting page with
 * the signed-in user's own requests, so the server's rules apply to each.
 */
import { useEffect, useRef, useSyncExternalStore } from 'react';

export const ASK_ASSISTANT = 'ask-assistant';
export const ADD_TO_WHITEBOARD = 'add-to-whiteboard';
export const OPEN_WHITEBOARD = 'open-whiteboard';

/**
 * The most text one action carries, so a long card or panel stays a reasonable question.
 * The chat frame holds the same limit (MAX_CONTEXT_CHARS in public/js/strands-chat.js).
 */
export const MAX_ACTION_TEXT = 6000;

// callId -> Map(type -> handler ref)
const handlers = new Map();
const listeners = new Set();
let revision = 0;

const changed = () => {
  revision += 1;
  listeners.forEach((listener) => listener());
};

/** Whether the page can do `type` for the meeting `callId` now. */
export const meetingActionAvailable = (callId, type) => Boolean(callId && handlers.get(callId)?.has(type));

/**
 * Ask the meeting page to do `type` for `callId`. Returns whether a page did.
 * The text is trimmed to MAX_ACTION_TEXT characters.
 */
export const requestMeetingAction = (callId, type, detail = {}) => {
  const handler = callId && handlers.get(callId)?.get(type);
  if (!handler?.current) return false;
  const text = typeof detail.text === 'string' ? detail.text.slice(0, MAX_ACTION_TEXT) : detail.text;
  handler.current({ ...detail, ...(text === undefined ? {} : { text }) });
  return true;
};

/**
 * Registers what the meeting page can do for `callId`: `actions` maps an
 * action type to its handler, or to null when the page cannot do it (the
 * assistant is turned off, the meeting is read-only). Re-registers when the
 * set of available actions changes; the handlers themselves can change freely.
 */
export const useMeetingActions = (callId, actions) => {
  const refs = useRef({});
  const types = Object.keys(actions)
    .filter((type) => typeof actions[type] === 'function')
    .sort();
  types.forEach((type) => {
    if (!refs.current[type]) refs.current[type] = { current: null };
    refs.current[type].current = actions[type];
  });
  const key = types.join(',');
  useEffect(() => {
    if (!callId || !key) return undefined;
    const own = new Map(key.split(',').map((type) => [type, refs.current[type]]));
    handlers.set(callId, own);
    changed();
    return () => {
      if (handlers.get(callId) === own) handlers.delete(callId);
      changed();
    };
  }, [callId, key]);
};

const subscribe = (listener) => {
  listeners.add(listener);
  return () => listeners.delete(listener);
};

/** Whether the page can do `type` for `callId`, re-rendering when that changes. */
export const useMeetingActionAvailable = (callId, type) => {
  useSyncExternalStore(subscribe, () => revision);
  return meetingActionAvailable(callId, type);
};

/** The quoted context of a guidance notification, for ASK_ASSISTANT and ADD_TO_WHITEBOARD. */
export const guidanceText = (notification) =>
  [notification?.Title, notification?.Body].filter((part) => typeof part === 'string' && part.trim()).join('\n\n');
