/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { generateClient } from 'aws-amplify/api';
import { ConsoleLogger } from 'aws-amplify/utils';
import onMeetingNotification from '../graphql/queries/onMeetingNotification';
import { isSharedWith } from '../components/common/shared-with';

const logger = new ConsoleLogger('useNotificationCount');

/** The most meetings the count keeps; the one heard from longest ago goes first. */
export const MAX_MEETINGS = 20;

/**
 * Whether a notification counts for this user: on a meeting they own or that is
 * shared with them, and not of a category they muted everywhere. An Admin
 * receives every meeting's notifications, and counts only their own meetings'.
 */
export const countsFor = (notification, username, mutedEverywhere = []) =>
  Boolean(username) &&
  (notification.Owner === username || isSharedWith(notification.SharedWith, username)) &&
  !mutedEverywhere.includes(notification.Category);

/**
 * The notifications that arrived during this visit to LMA, by meeting, for the
 * count in the top navigation. Nothing is stored: a notification published
 * while the user was not on LMA is not counted (it is in the meeting's own
 * feed), and the count starts again at a reload. The meeting open on the page
 * (`currentCallId`) is not counted, and opening a meeting clears its count.
 * `client` is for tests.
 */
const useNotificationCount = ({ username, currentCallId, mutedEverywhere = [], client: injected } = {}) => {
  const clientRef = useRef(injected || null);
  const [byMeeting, setByMeeting] = useState({});
  const contextRef = useRef({ username, currentCallId, mutedEverywhere });
  contextRef.current = { username, currentCallId, mutedEverywhere };

  useEffect(() => {
    if (!username) return undefined;
    if (!clientRef.current) clientRef.current = generateClient();
    let subscription;
    try {
      subscription = clientRef.current.graphql({ query: onMeetingNotification, variables: {} }).subscribe({
        next: (message) => {
          const notification = message?.data?.onMeetingNotification;
          const context = contextRef.current;
          if (!notification?.CallId || notification.CallId === context.currentCallId) return;
          if (!countsFor(notification, context.username, context.mutedEverywhere)) return;
          setByMeeting((current) => {
            const entry = current[notification.CallId] || { ids: [], last: 0 };
            if (entry.ids.includes(notification.NotificationId)) return current;
            const next = {
              ...current,
              [notification.CallId]: { ids: [...entry.ids, notification.NotificationId], last: Date.now() },
            };
            const callIds = Object.keys(next).sort((a, b) => next[b].last - next[a].last);
            return Object.fromEntries(callIds.slice(0, MAX_MEETINGS).map((callId) => [callId, next[callId]]));
          });
        },
        error: (err) => logger.error('notification count subscription failed', err),
      });
    } catch (err) {
      logger.error('could not subscribe to notifications', err);
    }
    return () => subscription?.unsubscribe?.();
  }, [username]);

  const clear = useCallback((callId) => {
    setByMeeting((current) => {
      if (!current[callId]) return current;
      const next = { ...current };
      delete next[callId];
      return next;
    });
  }, []);

  useEffect(() => {
    if (currentCallId) clear(currentCallId);
  }, [currentCallId, clear]);

  const meetings = Object.entries(byMeeting)
    .sort(([, a], [, b]) => b.last - a.last)
    .map(([callId, entry]) => ({ callId, count: entry.ids.length }));
  const total = meetings.reduce((sum, meeting) => sum + meeting.count, 0);
  return { total, meetings, clear };
};

export default useNotificationCount;
