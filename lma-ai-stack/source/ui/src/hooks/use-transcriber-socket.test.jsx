/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import { act, renderHook } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const socket = vi.hoisted(() => ({ url: null, options: null }));
const auth = vi.hoisted(() => ({ fetchAuthSession: null }));

vi.mock('react-use-websocket', () => ({
  default: (url, options) => {
    socket.url = url;
    socket.options = options;
    return { sendMessage: vi.fn() };
  },
}));
vi.mock('aws-amplify/auth', () => ({ fetchAuthSession: (...args) => auth.fetchAuthSession(...args) }));

// eslint-disable-next-line import/first
import useTranscriberSocket, { reconnectInterval, transcriberUrl } from './use-transcriber-socket';

const ENDPOINT = 'wss://transcriber.example/api/v1/ws';
const session = (access, id) => ({
  tokens: { accessToken: { toString: () => access }, idToken: { toString: () => id } },
});

describe('useTranscriberSocket', () => {
  beforeEach(() => {
    socket.url = null;
    socket.options = null;
    auth.fetchAuthSession = vi.fn();
  });

  it('fetches a session for every connection, so a reconnect presents current tokens', async () => {
    auth.fetchAuthSession
      .mockResolvedValueOnce(session('access-1', 'id-1'))
      .mockResolvedValueOnce(session('access-2', 'id-2'));
    renderHook(() => useTranscriberSocket({ endpoint: ENDPOINT, session: session('stale', 'stale') }));
    expect(await socket.url()).toBe(`${ENDPOINT}?authorization=Bearer%20access-1&id_token=id-1&refresh_token=`);
    expect(await socket.url()).toBe(`${ENDPOINT}?authorization=Bearer%20access-2&id_token=id-2&refresh_token=`);
  });

  it("uses the app's session when one cannot be fetched", async () => {
    auth.fetchAuthSession.mockRejectedValue(new Error('offline'));
    renderHook(() => useTranscriberSocket({ endpoint: ENDPOINT, session: session('cached', 'cached-id') }));
    expect(await socket.url()).toBe(transcriberUrl(ENDPOINT, 'cached', 'cached-id'));
  });

  it('keeps tokens out of the query parameters, whose change would reopen the socket', () => {
    renderHook(() => useTranscriberSocket({ endpoint: ENDPOINT, session: session('a', 'b') }));
    expect(socket.options.queryParams).toBeUndefined();
    expect(socket.options.shouldReconnect()).toBe(true);
  });

  it('does not connect without an endpoint', () => {
    renderHook(() => useTranscriberSocket({ endpoint: undefined }));
    expect(socket.url).toBeNull();
  });

  it('retries after 1, 2 and 4 seconds, then every 5', () => {
    expect([0, 1, 2, 3, 10].map(reconnectInterval)).toEqual([1000, 2000, 4000, 5000, 5000]);
  });

  it("sends the meeting's START on a reconnect before the page's own open frames", () => {
    const pageOpen = vi.fn((event) => event.target.send(JSON.stringify({ callEvent: 'CONSENT_SETTINGS' })));
    const { result } = renderHook(() => useTranscriberSocket({ endpoint: ENDPOINT, onOpen: pageOpen }));
    const first = {
      sent: [],
      send(data) {
        this.sent.push(data);
      },
    };
    act(() => socket.options.onOpen({ target: first }));
    result.current.begin({ callEvent: 'START', callId: 'm1', samplingRate: 16000 });
    act(() => socket.options.onClose({ target: first, code: 1006 }));

    const second = {
      sent: [],
      send(data) {
        this.sent.push(data);
      },
    };
    act(() => socket.options.onOpen({ target: second }));
    expect(second.sent.map((f) => JSON.parse(f).callEvent)).toEqual(['START', 'CONSENT_SETTINGS']);
    expect(JSON.parse(second.sent[0]).callId).toBe('m1');
    expect(pageOpen).toHaveBeenCalledTimes(2);
  });
});
