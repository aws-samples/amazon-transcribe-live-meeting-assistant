/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import { ConsoleLogger } from 'aws-amplify/utils';
import { useEffect, useState } from 'react';
import useAppContext from '../contexts/app';

const logger = new ConsoleLogger('useNotifications');

const dismissedInitialNotificationsStorageKey = 'dismissedInitialNotifications';
const initialNotifications = [
  {
    type: 'info',
    content: 'Welcome to Live Meeting Assistant',
    dismissible: true,
    dismissLabel: 'Dismiss message',
    id: 'welcome-1',
  },
];

const useNotifications = () => {
  const { errorMessage, setErrorMessage } = useAppContext();

  const [notifications, setNotifications] = useState([]);

  useEffect(() => {
    // sets initial notifications and persists state of dismissed notifications in local storage

    const getDissmissedNotificationIdsFromStorage = () => {
      let dismissedInitialNotificationIds = [];
      try {
        const dismissedStored = JSON.parse(localStorage.getItem(dismissedInitialNotificationsStorageKey) || '[]');
        if (!Array.isArray(dismissedStored)) {
          logger.warn('invalid format of dismisssed notifications from local storage');
        } else {
          dismissedInitialNotificationIds = dismissedStored;
        }
      } catch {
        logger.warn('failed to parse dismisssed notifications from local storage');
        return [];
      }

      return dismissedInitialNotificationIds;
    };

    const dismissedInitialNotificationIds = getDissmissedNotificationIdsFromStorage();
    const initialNotificationsNotDismissed = initialNotifications.filter(
      (n) => !dismissedInitialNotificationIds.includes(n.id),
    );

    const notificationIds = notifications.map((n) => n.id);
    // prettier-ignore
    if (
      // all have been dismissed
      !initialNotificationsNotDismissed.length
      // all area already in the notifications state
      || initialNotificationsNotDismissed.every((n) => notificationIds.includes(n.id))
    ) {
      return;
    }

    // Shown on the first visit only. Recorded as seen as soon as it is shown,
    // not only when it is dismissed: a welcome that comes back on every page
    // and every visit until somebody finds its close button is noise, and it
    // sits above the page content it pushes down. It stays for the rest of this
    // visit until dismissed.
    const seenIds = getDissmissedNotificationIdsFromStorage();
    const unseenIds = initialNotificationsNotDismissed.map((n) => n.id).filter((nId) => !seenIds.includes(nId));
    if (unseenIds.length) {
      try {
        localStorage.setItem(dismissedInitialNotificationsStorageKey, JSON.stringify([...seenIds, ...unseenIds]));
      } catch {
        logger.warn('failed to record the initial notifications as seen');
      }
    }

    // add dismiss handler to notifications
    const initialNotificationsToShow = initialNotificationsNotDismissed.map((n) => ({
      ...n,
      onDismiss: () => {
        setNotifications((current) => current.filter((i) => i.id !== n.id));
        const storedIds = getDissmissedNotificationIdsFromStorage().filter((i) => i !== n.id);
        localStorage.setItem(dismissedInitialNotificationsStorageKey, JSON.stringify([...storedIds, n.id]));
      },
    }));

    setNotifications((current) => [...initialNotificationsToShow, ...current]);
  }, [notifications]);

  useEffect(() => {
    // adds error messages to notifications
    const id = performance.now();
    const maxSameError = 3;
    const maxSameErrorInMs = 2000;

    if (!errorMessage) {
      return;
    }

    // limit the number of same error
    const sameErrorMessage = notifications.filter((i) => i.content === errorMessage);
    if (sameErrorMessage.lenth > maxSameError) {
      return;
    }
    // limit the number of errors within a time range
    const sameErrorInMs = sameErrorMessage.filter((i) => id - i.id > maxSameErrorInMs);
    if (sameErrorInMs.length) {
      return;
    }

    logger.debug('setting error notification', errorMessage);

    const errorNotification = {
      type: 'error',
      content: errorMessage,
      dismissible: true,
      dismissLabel: 'Dismiss message',
      id,
      onDismiss: () => {
        setNotifications((current) => current.filter((i) => i.id !== id));
      },
    };
    setNotifications((current) => [...current, errorNotification]);
    setErrorMessage('');
  }, [errorMessage, notifications]);

  return notifications;
};

export default useNotifications;
