/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * "Continue an existing meeting…" on the Stream Audio page, with the socket
 * and the AppSync client faked: reopenMeeting before streaming, then a START
 * under the meeting's ID naming the session, and the server's refusals shown.
 */
import React from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const socket = vi.hoisted(() => ({ options: null, worklet: null }));
const api = vi.hoisted(() => ({ graphql: null }));

vi.mock('react-use-websocket', () => ({
  default: (url, options) => {
    socket.options = options;
    return { sendMessage: vi.fn() };
  },
}));
vi.mock('aws-amplify/api', () => ({ generateClient: () => ({ graphql: (...args) => api.graphql(...args) }) }));
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

const CALL_ID = 'Weekly sync - 2026-11-19-10:00:00.000';

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
const startFrame = (target) => target.sent.find((f) => f.callEvent === 'START');

// Answers the reopen; anything else the page reads (the ASR engine) is empty.
const answering = (reopen) =>
  vi.fn(async ({ query }) => (query.includes('reopenMeeting') ? reopen() : { data: {} }));

const continueMeeting = (value) => {
  fireEvent.click(screen.getByText('Continue an existing meeting…'));
  fireEvent.change(screen.getByLabelText('Meeting ID or link'), { target: { value } });
};

describe('StreamAudio: continue an existing meeting', () => {
  beforeEach(() => {
    socket.worklet = null;
    api.graphql = answering(async () => ({
      data: { reopenMeeting: { CallId: CALL_ID, SessionNumber: 2, SessionOffsetSeconds: 317 } },
    }));
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
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("reopens the meeting, then streams under its ID naming the session the server allowed", async () => {
    render(<StreamAudio mode="stream" />);
    const target = openSocket();
    continueMeeting(`https://lma.example/#/calls/${encodeURIComponent(CALL_ID)}`);
    fireEvent.click(screen.getByRole('button', { name: 'Start Streaming' }));

    await waitFor(() => expect(startFrame(target)).toBeTruthy());
    const reopen = api.graphql.mock.calls.find(([{ query }]) => query.includes('reopenMeeting'));
    expect(reopen[0].variables).toEqual({ input: { CallId: CALL_ID } });
    expect(startFrame(target)).toMatchObject({ callId: CALL_ID, sessionNumber: 2, clientType: 'STREAM_AUDIO' });
    expect(screen.getByTestId('continue-session').textContent).toBe(
      `Continuing meeting ${CALL_ID} as session 2, from 5:17 on its transcript.`,
    );
  });

  it("shows the server's refusal and does not stream", async () => {
    api.graphql = answering(async () => {
      // eslint-disable-next-line no-throw-literal
      throw { errors: [{ errorType: 'MeetingInProgress', message: 'This meeting is still being captured.' }] };
    });
    render(<StreamAudio mode="stream" />);
    const target = openSocket();
    continueMeeting(CALL_ID);
    fireEvent.click(screen.getByRole('button', { name: 'Start Streaming' }));

    expect(await screen.findByText('This meeting is still being captured.')).toBeTruthy();
    expect(startFrame(target)).toBeUndefined();
    expect(window.navigator.mediaDevices.getDisplayMedia).not.toHaveBeenCalled();
  });

  it('asks for a meeting ID before calling the server', async () => {
    render(<StreamAudio mode="stream" />);
    openSocket();
    continueMeeting('https://example.com/not-a-meeting');
    fireEvent.click(screen.getByRole('button', { name: 'Start Streaming' }));
    expect(await screen.findByText("Paste the meeting ID, or the link to the meeting's page in LMA.")).toBeTruthy();
    expect(api.graphql.mock.calls.some(([{ query }]) => query.includes('reopenMeeting'))).toBe(false);
  });

  it("shows the transcriber's refusal of the session", async () => {
    render(<StreamAudio mode="stream" />);
    const target = openSocket();
    continueMeeting(CALL_ID);
    fireEvent.click(screen.getByRole('button', { name: 'Start Streaming' }));
    await waitFor(() => expect(startFrame(target)).toBeTruthy());

    const message = 'This meeting was not reopened for this capture. Choose to continue the meeting again, then start.';
    act(() =>
      socket.options.onMessage({
        data: JSON.stringify({
          event: 'CONSENT_STATE',
          captureAllowed: false,
          code: 'SESSION_NOT_ALLOWED',
          message,
        }),
      }),
    );
    expect(await screen.findByText(message)).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Start Streaming' })).toBeTruthy();
  });

  it('streams a new meeting, naming no session, when not continuing, also after a continuation', async () => {
    render(<StreamAudio mode="stream" />);
    const first = openSocket();
    continueMeeting(CALL_ID);
    fireEvent.click(screen.getByRole('button', { name: 'Start Streaming' }));
    await waitFor(() => expect(startFrame(first)).toBeTruthy());
    await waitFor(() => expect(socket.worklet?.port.onmessage).toBeTypeOf('function'));
    fireEvent.click(screen.getByRole('button', { name: 'Stop Streaming' }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Start Streaming' })).toBeTruthy());

    fireEvent.click(screen.getByText('Continue an existing meeting…'));
    const second = openSocket();
    fireEvent.click(screen.getByRole('button', { name: 'Start Streaming' }));
    await waitFor(() => expect(startFrame(second)).toBeTruthy());
    expect(startFrame(second)).not.toHaveProperty('sessionNumber');
    expect(startFrame(second).callId).not.toBe(CALL_ID);
  });
});
