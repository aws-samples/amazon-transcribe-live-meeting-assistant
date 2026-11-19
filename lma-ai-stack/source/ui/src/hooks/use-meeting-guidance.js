/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { generateClient } from 'aws-amplify/api';
import { ConsoleLogger } from 'aws-amplify/utils';

import onAddMeetingGuidance from '../graphql/queries/onAddMeetingGuidance';

const logger = new ConsoleLogger('useMeetingGuidance');

/** The most notes kept on the page at once; the oldest goes first. */
export const MAX_GUIDANCE_ITEMS = 5;

/**
 * Guidance pushed to the meeting by an MCP client while the page is open, newest
 * first, with a way to dismiss each one.
 *
 * AppSync delivers onAddMeetingGuidance only to the meeting's owner, the people
 * it is shared with and Admins (subscription.js). Dismissing a note hides it on
 * this page only; it stays stored with the meeting. `client` is for tests.
 */
const useMeetingGuidance = (callId, { client: injected } = {}) => {
  const clientRef = useRef(injected || null);
  const getClient = () => {
    if (!clientRef.current) clientRef.current = generateClient();
    return clientRef.current;
  };
  const [items, setItems] = useState([]);

  useEffect(() => {
    setItems([]);
    if (!callId) return undefined;
    let subscription;
    try {
      subscription = getClient()
        .graphql({ query: onAddMeetingGuidance, variables: { callId } })
        .subscribe({
          next: (message) => {
            const item = message?.data?.onAddMeetingGuidance;
            // AppSync filters on the CallId argument; this guards a stray message.
            if (!item?.GuidanceId || item.CallId !== callId) return;
            setItems((current) => {
              if (current.some((existing) => existing.GuidanceId === item.GuidanceId)) return current;
              return [item, ...current].slice(0, MAX_GUIDANCE_ITEMS);
            });
          },
          error: (err) => logger.error('meeting guidance subscription failed', err),
        });
    } catch (err) {
      logger.error('could not subscribe to meeting guidance', err);
    }
    return () => subscription?.unsubscribe?.();
  }, [callId]);

  const dismiss = useCallback((guidanceId) => {
    setItems((current) => current.filter((item) => item.GuidanceId !== guidanceId));
  }, []);

  return { items, dismiss };
};

export default useMeetingGuidance;
