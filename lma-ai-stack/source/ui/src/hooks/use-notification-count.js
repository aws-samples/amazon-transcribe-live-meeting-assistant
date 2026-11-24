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
import { meetingTitle } from '../components/common/meeting-header';

const logger = new ConsoleLogger('useNotificationCount');

/** The most meetings the count keeps; the one heard from longest ago goes first. */
export const MAX_MEETINGS = 20;

/**
 * How the top navigation names each meeting in `meetings`: its title as the
 * meeting page shows it (the Meeting ID without its time stamp; see
 * meetingTitle), read from the CallId itself, so naming them needs no request.
 * Meetings whose titles are the same, such as two days of "Weekly sync", keep
 * their whole Meeting ID, so every entry can be told apart.
 */
export const meetingLabels = (meetings) => {
  const titles = meetings.map((meeting) => meetingTitle(meeting.callId));
  const uses = titles.reduce((counts, title) => ({ ...counts, [title]: (counts[title] || 0) + 1 }), {});
  return titles.map((title, index) => (uses[title] > 1 ? meetings[index].callId : title));
};

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

  const counted = Object.entries(byMeeting)
    .sort(([, a], [, b]) => b.last - a.last)
    .map(([callId, entry]) => ({ callId, count: entry.ids.length }));
  const labels = meetingLabels(counted);
  const meetings = counted.map((meeting, index) => ({ ...meeting, label: labels[index] }));
  const total = meetings.reduce((sum, meeting) => sum + meeting.count, 0);
  return { total, meetings, clear };
};

export default useNotificationCount;
