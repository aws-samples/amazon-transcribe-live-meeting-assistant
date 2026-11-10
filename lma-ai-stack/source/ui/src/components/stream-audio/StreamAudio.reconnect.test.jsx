/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The Stream Audio page across a reconnect of its transcriber socket, with the
 * socket faked.
 *
 * - Every connection that opens during a meeting gets the meeting's START, the
 *   same one, before any audio; the first gets exactly one.
 * - A connection that stays down past the transcriber's hold ends the stream
 *   with a notice and no END.
 */
import React from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { RESUME_WINDOW_MS } from './transcriber-stream';

const socket = vi.hoisted(() => ({ options: null, worklet: null }));

vi.mock('react-use-websocket', () => ({
  default: (url, options) => {
    socket.options = options;
    return { sendMessage: vi.fn() };
  },
}));
vi.mock('aws-amplify/api', () => ({ generateClient: () => ({ graphql: vi.fn().mockResolvedValue({ data: {} }) }) }));
vi.mock('aws-amplify/auth', () => ({
  fetchUserAttributes: vi.fn().mockResolvedValue({}),
  fetchAuthSession: vi.fn().mockResolvedValue({}),
}));
vi.mock('../../contexts/app', () => ({
  default: () => ({
    currentSession: { tokens: { accessToken: 'access', idToken: 'id' } },
    user: { username: 'ana' },
    authState: 'authenticated',
  }),
}));
vi.mock('../../contexts/settings', () => ({
  default: () => ({ settings: { WSEndpoint: 'wss://transcriber.example/api/v1/ws', recordingDisclaimer: 'OK?' } }),
}));

// eslint-disable-next-line import/first
import StreamAudio from './StreamAudio';

const node = () => ({ connect: vi.fn(), disconnect: vi.fn() });
const fakeStream = () => ({ getVideoTracks: () => [], getAudioTracks: () => [{ enabled: true }], getTracks: () => [] });

function FakeAudioContext() {
  return {
    sampleRate: 16000,
    audioWorklet: { addModule: vi.fn().mockResolvedValue(undefined) },
    createMediaStreamSource: () => node(),
    createChannelSplitter: () => node(),
    createChannelMerger: () => node(),
  };
}
function FakeWorkletNode() {
  const worklet = { port: { postMessage: vi.fn(), close: vi.fn() }, connect: vi.fn(), disconnect: vi.fn() };
  socket.worklet = worklet;
  return worklet;
}

// A socket that records frames as their callEvent, and audio as 'audio'.
const openSocket = () => {
  const target = {
    sent: [],
    send(message) {
      this.sent.push(typeof message === 'string' ? JSON.parse(message) : 'audio');
    },
  };
  act(() => socket.options.onOpen({ target }));
  return target;
};
const closeSocket = (target) => act(() => socket.options.onClose({ target, code: 1006 }));
const kinds = (target) => target.sent.map((f) => (f === 'audio' ? f : f.callEvent));
const captureAudio = () => act(() => socket.worklet.port.onmessage({ data: new Int16Array(256) }));

const startStreaming = async (target) => {
  fireEvent.click(screen.getByRole('button', { name: 'Start Streaming' }));
  await waitFor(() => expect(kinds(target)).toContain('START'));
  await waitFor(() => expect(socket.worklet?.port.onmessage).toBeTypeOf('function'));
};

describe('StreamAudio reconnect', () => {
  beforeEach(() => {
    socket.worklet = null;
    vi.stubGlobal('AudioContext', FakeAudioContext);
    vi.stubGlobal('AudioWorkletNode', FakeWorkletNode);
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    Object.defineProperty(window.navigator, 'mediaDevices', {
      configurable: true,
      value: {
        getDisplayMedia: vi.fn().mockResolvedValue(fakeStream()),
        getUserMedia: vi.fn().mockResolvedValue(fakeStream()),
      },
    });
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('sends one START on the first connection, and the same START first on a reconnect', async () => {
    render(<StreamAudio mode="stream" />);
    const first = openSocket();
    await startStreaming(first);
    captureAudio();
    expect(kinds(first)).toEqual(['CONSENT_SETTINGS', 'START', 'audio']);

    closeSocket(first);
    captureAudio(); // held while disconnected
    const second = openSocket();
    captureAudio();

    expect(kinds(second)).toEqual(['START', 'audio', 'CONSENT_SETTINGS', 'audio']);
    const start = first.sent.find((f) => f.callEvent === 'START');
    expect(second.sent[0]).toEqual(start);
    expect(start.samplingRate).toBe(16000);
    // The speaker names travel under the names the streaming API documents.
    expect(start.localSpeakerName).toEqual(expect.any(String));
    expect(start.remoteSpeakerName).toEqual(expect.any(String));
    ['agentId', 'fromNumber', 'toNumber'].forEach((name) => expect(start).not.toHaveProperty(name));
    expect(kinds(first)).toEqual(['CONSENT_SETTINGS', 'START', 'audio']);
  });

  it('sends END on the current connection when stopped after a reconnect', async () => {
    render(<StreamAudio mode="stream" />);
    const first = openSocket();
    await startStreaming(first);
    closeSocket(first);
    const second = openSocket();
    fireEvent.click(screen.getByRole('button', { name: 'Stop Streaming' }));
    await waitFor(() => expect(kinds(second)).toContain('END'));
    expect(kinds(first)).not.toContain('END');
  });

  it('ends the stream with a notice, and no END, when the connection stays down past the hold', async () => {
    render(<StreamAudio mode="stream" />);
    const first = openSocket();
    await startStreaming(first);

    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
    closeSocket(first);
    act(() => vi.advanceTimersByTime(RESUME_WINDOW_MS));
    vi.useRealTimers();

    expect(await screen.findByText('Connection lost: meeting ended')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Start Streaming' })).toBeTruthy();
    expect(screen.getByText('Open recorded meeting')).toBeTruthy();

    // The connection coming back sends nothing for the ended meeting.
    const second = openSocket();
    expect(kinds(second)).toEqual(['CONSENT_SETTINGS']);
    expect(kinds(first)).not.toContain('END');
  });
});
