/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { generateClient } from 'aws-amplify/api';
import { ConsoleLogger } from 'aws-amplify/utils';
import {
  getMyNotificationPreferences,
  updateMyNotificationPreferences,
} from '../graphql/queries/meetingNotificationState';

const logger = new ConsoleLogger('useNotificationPreferences');

/** Fired on window when this page changes the user's muted categories, so every user of this hook agrees. */
export const PREFERENCES_EVENT = 'lma-notification-preferences';

/**
 * The categories of meeting notifications the signed-in user has muted on every
 * meeting, stored with their account (getMyNotificationPreferences), with a way
 * to change them. Muting hides a category's notifications from this user's
 * meeting pages and from the count in the top navigation; they stay stored and
 * other people still see them. `client` is for tests.
 */
const useNotificationPreferences = ({ client: injected, enabled = true } = {}) => {
  const clientRef = useRef(injected || null);
  const getClient = () => {
    if (!clientRef.current) clientRef.current = generateClient();
    return clientRef.current;
  };
  const [muted, setMuted] = useState([]);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    if (!enabled) return undefined;
    let cancelled = false;
    getClient()
      .graphql({ query: getMyNotificationPreferences })
      .then((response) => {
        if (!cancelled) setMuted(response?.data?.getMyNotificationPreferences?.MutedCategories || []);
      })
      .catch((err) => logger.error('could not read the notification preferences', err))
      .finally(() => {
        if (!cancelled) setLoaded(true);
      });
    const onChange = (event) => setMuted(event.detail || []);
    window.addEventListener(PREFERENCES_EVENT, onChange);
    return () => {
      cancelled = true;
      window.removeEventListener(PREFERENCES_EVENT, onChange);
    };
  }, [enabled]);

  const change = useCallback(async (field, category) => {
    const response = await getClient().graphql({
      query: updateMyNotificationPreferences,
      variables: { input: { [field]: [category] } },
    });
    const next = response?.data?.updateMyNotificationPreferences?.MutedCategories || [];
    setMuted(next);
    window.dispatchEvent(new CustomEvent(PREFERENCES_EVENT, { detail: next }));
    return next;
  }, []);

  const muteEverywhere = useCallback((category) => change('Mute', category), [change]);
  const unmuteEverywhere = useCallback((category) => change('Unmute', category), [change]);

  return { mutedEverywhere: muted, loaded, muteEverywhere, unmuteEverywhere };
};

export default useNotificationPreferences;
