/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The embeddable Stream Audio component's transcriber socket, faked: a meeting
 * the transcriber ended while disconnected is reported to the parent page with
 * reason CONNECTION_LOST, and a meeting the user stopped is reported without one.
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
  default: () => ({
    settings: { WSEndpoint: 'wss://transcriber.example/api/v1/ws', recordingDisclaimer: 'Record?' },
  }),
}));

// eslint-disable-next-line import/first
import EmbedStreamAudio from './EmbedStreamAudio';

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

const stoppedMessages = (sendToParent) =>
  sendToParent.mock.calls.map(([m]) => m).filter((m) => m.type === 'LMA_MEETING_STOPPED');

describe('EmbedStreamAudio', () => {
  let sendToParent;

  beforeEach(() => {
    sendToParent = vi.fn();
    vi.stubGlobal('AudioContext', FakeAudioContext);
    vi.stubGlobal('AudioWorkletNode', FakeWorkletNode);
    vi.stubGlobal('confirm', () => true);
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
    render(<EmbedStreamAudio params={{ meetingTopic: 'Weekly' }} sendToParent={sendToParent} />);
    const first = openSocket();
    act(() => window.dispatchEvent(new MessageEvent('message', { data: { type: 'LMA_START_MEETING' } })));
    await screen.findByRole('button', { name: 'Stop Streaming' });
    await waitFor(() => expect(first.sent).toEqual(['START']));
    return first;
  };

  it('tells the parent the meeting ended for a lost connection', async () => {
    const first = await start();
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
    closeSocket(first);
    act(() => vi.advanceTimersByTime(RESUME_WINDOW_MS));
    vi.useRealTimers();

    expect(await screen.findByText('Connection lost: meeting ended')).toBeTruthy();
    await waitFor(() => expect(stoppedMessages(sendToParent)).toHaveLength(1));
    expect(stoppedMessages(sendToParent)[0]).toEqual(expect.objectContaining({ reason: 'CONNECTION_LOST' }));
    expect(openSocket().sent).toEqual([], 'nothing more is sent for the ended meeting');
  });

  it('tells the parent the user stopped the meeting, with no reason', async () => {
    const first = await start();
    fireEvent.click(screen.getByRole('button', { name: 'Stop Streaming' }));
    await waitFor(() => expect(stoppedMessages(sendToParent)).toHaveLength(1));
    expect(Object.keys(stoppedMessages(sendToParent)[0]).sort()).toEqual(['callId', 'type']);
    expect(first.sent).toEqual(['START', 'END']);
  });
});
