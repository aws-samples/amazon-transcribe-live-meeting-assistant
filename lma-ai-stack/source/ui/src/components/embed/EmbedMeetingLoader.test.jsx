/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The embeddable meeting loader across a reconnect of its transcriber socket,
 * with the socket faked: the same START on the new connection, and a meeting
 * the transcriber ended while disconnected reported to the parent page.
 */
import React from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { RESUME_WINDOW_MS } from '../stream-audio/transcriber-stream';

const socket = vi.hoisted(() => ({ options: null }));

vi.mock('react-use-websocket', () => ({
  default: (url, options) => {
    socket.options = options;
    return { sendMessage: vi.fn() };
  },
}));
vi.mock('aws-amplify/auth', () => ({ fetchAuthSession: vi.fn().mockResolvedValue({}) }));
vi.mock('../../contexts/app', () => ({ default: () => ({ currentSession: {}, user: { username: 'ana' } }) }));
vi.mock('../../contexts/settings', () => ({
  default: () => ({ settings: { WSEndpoint: 'wss://transcriber.example/api/v1/ws' } }),
}));

// eslint-disable-next-line import/first
import EmbedMeetingLoader from './EmbedMeetingLoader';

const node = () => ({ connect: vi.fn(), disconnect: vi.fn() });
const fakeStream = () => ({ getVideoTracks: () => [], getAudioTracks: () => [{ enabled: true }], getTracks: () => [] });
function FakeAudioContext() {
  return {
    sampleRate: 48000,
    audioWorklet: { addModule: vi.fn().mockResolvedValue(undefined) },
    createMediaStreamSource: () => node(),
    createChannelSplitter: () => node(),
    createChannelMerger: () => node(),
  };
}
function FakeWorkletNode() {
  return { port: { postMessage: vi.fn(), close: vi.fn() }, connect: vi.fn(), disconnect: vi.fn() };
}

const openSocket = () => {
  const target = {
    sent: [],
    send(message) {
      this.sent.push(JSON.parse(message).callEvent);
    },
  };
  act(() => socket.options.onOpen({ target }));
  return target;
};
const closeSocket = (target) => act(() => socket.options.onClose({ target, code: 1006 }));

describe('EmbedMeetingLoader reconnect', () => {
  let sendToParent;

  beforeEach(() => {
    sendToParent = vi.fn();
    vi.stubGlobal('AudioContext', FakeAudioContext);
    vi.stubGlobal('AudioWorkletNode', FakeWorkletNode);
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

  const start = async () => {
    render(<EmbedMeetingLoader params={{ meetingTopic: 'Weekly' }} sendToParent={sendToParent} />);
    const first = openSocket();
    // Started by the parent page, which calls startMeeting once.
    act(() => window.dispatchEvent(new MessageEvent('message', { data: { type: 'LMA_START_MEETING' } })));
    await screen.findByText('Meeting In Progress');
    return first;
  };

  it('sends the START again on a reconnect, and END with a plain stop message', async () => {
    const first = await start();
    expect(first.sent).toEqual(['START']);
    closeSocket(first);
    const second = openSocket();
    expect(second.sent).toEqual(['START']);

    fireEvent.click(screen.getByRole('button', { name: 'Stop Meeting' }));
    await screen.findByText('Meeting Ended');
    expect(second.sent).toEqual(['START', 'END']);
    const stopped = sendToParent.mock.calls.map(([m]) => m).find((m) => m.type === 'LMA_MEETING_STOPPED');
    expect(Object.keys(stopped).sort()).toEqual(['callId', 'type']);
  });

  it('tells the parent the meeting ended for a lost connection', async () => {
    const first = await start();
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
    closeSocket(first);
    act(() => vi.advanceTimersByTime(RESUME_WINDOW_MS));
    vi.useRealTimers();

    expect(await screen.findByText('Connection lost: meeting ended')).toBeTruthy();
    await waitFor(() =>
      expect(sendToParent).toHaveBeenCalledWith(
        expect.objectContaining({ type: 'LMA_MEETING_STOPPED', reason: 'CONNECTION_LOST' }),
      ),
    );
    expect(openSocket().sent).toEqual([]);
  });
});
