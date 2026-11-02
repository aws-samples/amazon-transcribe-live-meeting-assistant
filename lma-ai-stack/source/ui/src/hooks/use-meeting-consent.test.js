/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import { describe, expect, it, vi } from 'vitest';
import { act, renderHook, waitFor } from '@testing-library/react';

import useMeetingConsent, { READ_DELAY_MS } from './use-meeting-consent';

const fakeClient = () => {
  const handlers = {};
  let version = 1;
  const client = {
    graphql: vi.fn(({ query, variables }) => {
      if (query.includes('subscription OnUpdateMeetingConsent')) {
        return {
          subscribe: (observer) => {
            handlers.next = observer.next;
            return { unsubscribe: vi.fn() };
          },
        };
      }
      if (query.includes('query GetMeetingConsent')) {
        return Promise.resolve({
          data: {
            getMeetingConsent: {
              CallId: variables.callId,
              CaptureAllowed: false,
              Settings: { Mode: 'opt_in' },
              Consent: { Mode: 'opt_in', Status: 'pending', Version: version },
            },
          },
        });
      }
      version += 1;
      return Promise.resolve({ data: {} });
    }),
  };
  return { client, handlers };
};

describe('useMeetingConsent', () => {
  it('reads the record and reads it again on each change announced', async () => {
    const { client, handlers } = fakeClient();
    const { result } = renderHook(() => useMeetingConsent('m1', { client }));
    await waitFor(() => expect(result.current.consent?.Version).toBe(1));
    expect(result.current.captureAllowed).toBe(false);
    expect(result.current.settings.Mode).toBe('opt_in');
    client.graphql.mockClear();
    vi.useFakeTimers();
    try {
      // The version already held is not read again.
      await act(async () => handlers.next({ data: { onUpdateMeetingConsent: { Consent: { Version: 1 } } } }));
      await act(async () => vi.advanceTimersByTime(READ_DELAY_MS));
      expect(client.graphql).not.toHaveBeenCalled();
      // A burst of newer versions is read once.
      await act(async () => {
        handlers.next({ data: { onUpdateMeetingConsent: { Consent: { Version: 2 } } } });
        handlers.next({ data: { onUpdateMeetingConsent: { Consent: { Version: 3 } } } });
      });
      await act(async () => vi.advanceTimersByTime(READ_DELAY_MS));
    } finally {
      vi.useRealTimers();
    }
    expect(client.graphql).toHaveBeenCalledTimes(1);
    expect(client.graphql.mock.calls[0][0].query).toContain('query GetMeetingConsent');
  });

  it('records an objection under the meeting and reads the record again', async () => {
    const { client } = fakeClient();
    const { result } = renderHook(() => useMeetingConsent('m1', { client }));
    await waitFor(() => expect(result.current.consent).not.toBeNull());
    let outcome;
    await act(async () => {
      outcome = await result.current.recordObjection('Bo Chen', 'please stop');
    });
    expect(outcome).toBeNull();
    const sent = client.graphql.mock.calls.find(([call]) => call.query.includes('mutation RecordMeetingConsentEvent'));
    expect(sent[0].variables.input).toEqual({
      CallId: 'm1',
      ParticipantName: 'Bo Chen',
      Decision: 'objection',
      Text: 'please stop',
    });
    expect(result.current.consent.Version).toBe(2);
  });

  it('answers a refused change with its message', async () => {
    const { client } = fakeClient();
    const { result } = renderHook(() => useMeetingConsent('m1', { client }));
    await waitFor(() => expect(result.current.consent).not.toBeNull());
    client.graphql.mockImplementationOnce(() =>
      Promise.reject(Object.assign(new Error('GraphQL error'), { errors: [{ message: 'Unauthorized' }] })),
    );
    let outcome;
    await act(async () => {
      outcome = await result.current.attest('verbal');
    });
    expect(outcome).toBe('Unauthorized');
  });
});
