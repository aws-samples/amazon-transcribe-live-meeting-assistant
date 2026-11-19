/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { generateClient } from 'aws-amplify/api';
import { ConsoleLogger } from 'aws-amplify/utils';
import listMeetingNotifications from '../graphql/queries/listMeetingNotifications';
import onMeetingNotification from '../graphql/queries/onMeetingNotification';
import {
  getMyMeetingNotificationState,
  updateMyMeetingNotificationState,
} from '../graphql/queries/meetingNotificationState';
import useNotificationPreferences from './use-notification-preferences';

const logger = new ConsoleLogger('useMeetingNotifications');

/** The most notifications the page loads for a meeting, a page of PAGE_SIZE at a time. */
export const PAGE_SIZE = 200;
export const MAX_LOADED = 1000;

/** Time order: when first published, then the id, so two at the same moment keep one order. */
export const byCreatedAt = (a, b) =>
  (a.CreatedAt || '').localeCompare(b.CreatedAt || '') ||
  (a.NotificationId || '').localeCompare(b.NotificationId || '');

/** Whether a notification's ExpiresAt is past. */
export const isExpired = (notification, now) =>
  Boolean(notification.ExpiresAt) && Date.parse(notification.ExpiresAt) <= now;

/**
 * Adds or replaces one notification in a map keyed by NotificationId. An update
 * made through a DedupKey keeps its id and carries a higher Revision; an older
 * copy arriving late does not replace a newer one.
 */
export const mergeNotification = (map, notification) => {
  if (!notification?.NotificationId) return map;
  const existing = map[notification.NotificationId];
  if (existing && (existing.Revision || 0) > (notification.Revision || 0)) return map;
  return { ...map, [notification.NotificationId]: notification };
};

/**
 * A meeting's notifications, as the meeting page shows them: those published
 * before the page opened (listMeetingNotifications), then each one published
 * or updated while it is open (onMeetingNotification), in time order.
 *
 * AppSync answers both only for the meeting's owner, the people it is shared
 * with and Admins, decided from the meeting as it is now. Dismissing a
 * notification and muting a category change only this user's view; they are
 * stored with the user (getMyMeetingNotificationState, and
 * getMyNotificationPreferences for categories muted on every meeting).
 *
 * `liveArrivals` counts the notifications that arrived or changed while the
 * page was open and that this user would see, so the page can open the panel
 * or mark its trigger. `client` and `now` are for tests.
 */
const useMeetingNotifications = (callId, { client: injected, now: nowForTests } = {}) => {
  const clientRef = useRef(injected || null);
  const getClient = () => {
    if (!clientRef.current) clientRef.current = generateClient();
    return clientRef.current;
  };
  const [byId, setById] = useState({});
  const [dismissed, setDismissed] = useState([]);
  const [mutedHere, setMutedHere] = useState([]);
  const [liveArrivals, setLiveArrivals] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [tick, setTick] = useState(Date.now());
  const { mutedEverywhere, muteEverywhere, unmuteEverywhere } = useNotificationPreferences({
    client: injected,
    enabled: Boolean(callId),
  });
  const hiddenRef = useRef({ dismissed: [], mutedHere: [], mutedEverywhere: [] });
  hiddenRef.current = { dismissed, mutedHere, mutedEverywhere };

  useEffect(() => {
    setById({});
    setDismissed([]);
    setMutedHere([]);
    setLiveArrivals(0);
    setError(null);
    if (!callId) return undefined;
    let cancelled = false;
    const client = getClient();

    const load = async () => {
      setLoading(true);
      let nextToken = null;
      let loaded = 0;
      try {
        do {
          // eslint-disable-next-line no-await-in-loop
          const response = await client.graphql({
            query: listMeetingNotifications,
            // Newest first, so that past MAX_LOADED the oldest are the ones left out.
            variables: { callId, limit: PAGE_SIZE, nextToken, newestFirst: true },
          });
          const page = response?.data?.listMeetingNotifications || {};
          const items = (page.Items || []).filter(Boolean);
          loaded += items.length;
          if (cancelled) return;
          setById((current) => items.reduce(mergeNotification, current));
          nextToken = page.NextToken || null;
        } while (nextToken && loaded < MAX_LOADED);
      } catch (err) {
        logger.error('could not load the meeting notifications', err);
        if (!cancelled) setError('The notifications could not be loaded.');
      } finally {
        if (!cancelled) setLoading(false);
      }
    };

    const loadState = async () => {
      try {
        const response = await client.graphql({ query: getMyMeetingNotificationState, variables: { callId } });
        const state = response?.data?.getMyMeetingNotificationState || {};
        if (cancelled) return;
        setDismissed(state.Dismissed || []);
        setMutedHere(state.MutedCategories || []);
      } catch (err) {
        logger.error('could not load this meeting notification view', err);
      }
    };

    let subscription;
    try {
      subscription = client.graphql({ query: onMeetingNotification, variables: { callId } }).subscribe({
        next: (message) => {
          const notification = message?.data?.onMeetingNotification;
          // AppSync filters on the CallId argument; this guards a stray message.
          if (!notification?.NotificationId || notification.CallId !== callId) return;
          setById((current) => mergeNotification(current, notification));
          const hidden = hiddenRef.current;
          if (
            !hidden.dismissed.includes(notification.NotificationId) &&
            !hidden.mutedHere.includes(notification.Category) &&
            !hidden.mutedEverywhere.includes(notification.Category)
          ) {
            setLiveArrivals((count) => count + 1);
          }
        },
        error: (err) => logger.error('meeting notification subscription failed', err),
      });
    } catch (err) {
      logger.error('could not subscribe to meeting notifications', err);
    }
    load();
    loadState();
    return () => {
      cancelled = true;
      subscription?.unsubscribe?.();
    };
  }, [callId]);

  const items = useMemo(() => Object.values(byId).sort(byCreatedAt), [byId]);

  // Notifications with an ExpiresAt leave the page when it passes.
  useEffect(() => {
    if (nowForTests !== undefined || !items.some((item) => item.ExpiresAt)) return undefined;
    const timer = setInterval(() => setTick(Date.now()), 30000);
    return () => clearInterval(timer);
  }, [items, nowForTests]);
  const now = nowForTests ?? tick;

  const isMuted = useCallback(
    (category) => mutedHere.includes(category) || mutedEverywhere.includes(category),
    [mutedHere, mutedEverywhere],
  );
  const visible = useMemo(
    () =>
      items.filter(
        (item) => !dismissed.includes(item.NotificationId) && !isMuted(item.Category) && !isExpired(item, now),
      ),
    [items, dismissed, isMuted, now],
  );

  const changeState = useCallback(
    async (input) => {
      const response = await getClient().graphql({
        query: updateMyMeetingNotificationState,
        variables: { input: { CallId: callId, ...input } },
      });
      const state = response?.data?.updateMyMeetingNotificationState;
      if (state) {
        setDismissed(state.Dismissed || []);
        setMutedHere(state.MutedCategories || []);
      }
    },
    [callId],
  );

  const keepOrRollBack = (promise, rollBack) =>
    promise.catch((err) => {
      logger.error('could not save the change to the notifications', err);
      rollBack();
      setError('The change could not be saved.');
    });

  const dismiss = useCallback(
    (id) => {
      setDismissed((current) => (current.includes(id) ? current : [...current, id]));
      return keepOrRollBack(changeState({ Dismiss: [id] }), () =>
        setDismissed((current) => current.filter((existing) => existing !== id)),
      );
    },
    [changeState],
  );
  const restore = useCallback(
    (id) => {
      setDismissed((current) => current.filter((existing) => existing !== id));
      return keepOrRollBack(changeState({ Restore: [id] }), () => setDismissed((current) => [...current, id]));
    },
    [changeState],
  );
  const muteHere = useCallback(
    (category) => {
      setMutedHere((current) => (current.includes(category) ? current : [...current, category]));
      return keepOrRollBack(changeState({ Mute: [category] }), () =>
        setMutedHere((current) => current.filter((existing) => existing !== category)),
      );
    },
    [changeState],
  );
  const unmuteHere = useCallback(
    (category) => {
      setMutedHere((current) => current.filter((existing) => existing !== category));
      return keepOrRollBack(changeState({ Unmute: [category] }), () =>
        setMutedHere((current) => [...current, category]),
      );
    },
    [changeState],
  );
  const muteEverywhereSafely = useCallback(
    (category) => keepOrRollBack(muteEverywhere(category), () => {}),
    [muteEverywhere],
  );
  const unmuteEverywhereSafely = useCallback(
    (category) => keepOrRollBack(unmuteEverywhere(category), () => {}),
    [unmuteEverywhere],
  );

  return {
    items,
    visible,
    dismissed,
    mutedHere,
    mutedEverywhere,
    isMuted,
    liveArrivals,
    loading,
    error,
    now,
    client: getClient,
    dismiss,
    restore,
    muteHere,
    unmuteHere,
    muteEverywhere: muteEverywhereSafely,
    unmuteEverywhere: unmuteEverywhereSafely,
  };
};

export default useMeetingNotifications;
