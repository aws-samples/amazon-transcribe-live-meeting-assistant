/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import { useCallback, useEffect, useRef, useState } from 'react';

export const NOTIFICATIONS_DRAWER_ID = 'meeting-notifications';
export const SIZE_STORAGE_KEY = 'lma-meeting-notifications-size';
export const DEFAULT_SIZE = 360;

const readSize = (storage) => {
  try {
    const size = Number(storage?.getItem(SIZE_STORAGE_KEY));
    return Number.isFinite(size) && size >= 200 ? size : DEFAULT_SIZE;
  } catch {
    return DEFAULT_SIZE;
  }
};

/**
 * Whether the meeting page's Notifications drawer is open, its width, and
 * whether its trigger shows a badge.
 *
 * The drawer opens by itself when a notification arrives while the page is
 * open, unless the user has closed it on this meeting during this visit; then
 * the trigger shows a badge instead, until the drawer is opened. Its width is
 * remembered in this browser. `liveArrivals` is useMeetingNotifications'
 * count of notifications that arrived while the page was open.
 */
const useNotificationsDrawer = ({ callId, liveArrivals, storage = window.localStorage }) => {
  const [open, setOpen] = useState(false);
  const [badge, setBadge] = useState(false);
  const [size, setSize] = useState(() => readSize(storage));
  const closedOn = useRef(new Set());
  const seenArrivals = useRef(0);

  useEffect(() => {
    setOpen(false);
    setBadge(false);
    seenArrivals.current = 0;
  }, [callId]);

  useEffect(() => {
    if (!callId || liveArrivals <= seenArrivals.current) {
      seenArrivals.current = liveArrivals;
      return;
    }
    seenArrivals.current = liveArrivals;
    if (open) return;
    if (closedOn.current.has(callId)) {
      setBadge(true);
    } else {
      setOpen(true);
    }
  }, [callId, liveArrivals, open]);

  const change = useCallback(
    (next) => {
      setOpen(next);
      if (next) {
        setBadge(false);
      } else if (callId) {
        closedOn.current.add(callId);
      }
    },
    [callId],
  );

  // Put away for another drawer (Help), which is not the user closing it.
  const hide = useCallback(() => setOpen(false), []);

  const resize = useCallback(
    (next) => {
      setSize(next);
      try {
        storage?.setItem(SIZE_STORAGE_KEY, String(Math.round(next)));
      } catch {
        // Storage unavailable: the width lasts for this page only.
      }
    },
    [storage],
  );

  return { open, setOpen: change, hide, badge, size, resize };
};

export default useNotificationsDrawer;
