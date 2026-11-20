/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import { vi } from 'vitest';

/**
 * A stand-in for the Amplify GraphQL client in the meeting notification tests:
 * queries and mutations are answered from `answers` (by operation field name),
 * and the subscription's observer is kept so a test can send it messages.
 */
const fakeClient = (answers = {}) => {
  const state = { observers: [], requests: [] };
  const unsubscribe = vi.fn();
  const client = {
    graphql: vi.fn(({ query, variables }) => {
      state.requests.push({ query, variables });
      if (query.trim().startsWith('subscription')) {
        return {
          subscribe: (observer) => {
            state.observers.push({ observer, variables });
            return { unsubscribe };
          },
        };
      }
      const name = Object.keys(answers).find((field) => query.includes(`${field}(`) || query.includes(`${field} {`));
      const answer = name ? answers[name] : undefined;
      if (answer instanceof Error) return Promise.reject(answer);
      const data = typeof answer === 'function' ? answer(variables) : answer;
      return Promise.resolve({ data: name ? { [name]: data } : {} });
    }),
  };
  const send = (notification) =>
    state.observers.forEach(({ observer }) => observer.next({ data: { onMeetingNotification: notification } }));
  return { client, state, send, unsubscribe };
};

export const notification = (id, extra = {}) => ({
  CallId: 'm1',
  NotificationId: id,
  Category: 'guidance',
  Type: 'ask_question',
  Severity: 'SUGGESTION',
  Title: `Title ${id}`,
  Body: `Body ${id}`,
  Refs: [],
  DedupKey: null,
  Actions: [],
  Source: 'guidance-analyzer',
  CreatedBy: null,
  CreatedAt: '2026-11-19T14:00:00.000Z',
  UpdatedAt: '2026-11-19T14:00:00.000Z',
  Revision: 1,
  ExpiresAt: null,
  Owner: 'owner@example.com',
  SharedWith: '[shared@example.com]',
  ...extra,
});

export default fakeClient;
