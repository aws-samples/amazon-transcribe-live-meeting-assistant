/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The Stream Audio/Video page's screen video, with the transcriber's sockets,
 * the screen capture and MediaRecorder faked.
 *
 * - The switch is shown only when the settings answer allows video for this
 *   page, and is off at the start of every meeting.
 * - With it on, the share is asked for at 5 frames a second, the surface is
 *   named, a whole screen is warned about, and the video goes over its own
 *   connection (START_VIDEO with the meeting's callId, chunks, END_VIDEO).
 * - Stop video ends the video and keeps the audio streaming.
 * - The consent notice says the screen is recorded, and an opt-in meeting's
 *   video waits for its consent to be granted.
 */
import React from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const socket = vi.hoisted(() => ({ options: null, sent: [] }));

vi.mock('react-use-websocket', () => ({
  default: (url, options) => {
    socket.options = options;
    return { sendMessage: (message) => socket.sent.push(message) };
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
// eslint-disable-next-line import/first
import { DEFAULT_VIDEO_NOTICE_TEXT, VIDEO_CONSTRAINTS } from './screen-video';

const node = () => ({ connect: vi.fn(), disconnect: vi.fn() });

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

const media = vi.hoisted(() => ({ recorders: [], sockets: [], surface: 'browser' }));

function FakeMediaRecorder(stream, options) {
  this.stream = stream;
  this.options = options;
  this.state = 'inactive';
  this.start = (slice) => {
    this.state = 'recording';
    this.slice = slice;
  };
  this.stop = () => {
    this.state = 'inactive';
    this.onstop?.();
  };
  media.recorders.push(this);
}
FakeMediaRecorder.isTypeSupported = () => true;

function FakeWebSocket(url) {
  this.url = url;
  this.sent = [];
  this.send = (data) => this.sent.push(data);
  this.close = (code) => {
    this.closedWith = code;
    setTimeout(() => this.onclose?.({ code }), 0);
  };
  media.sockets.push(this);
}
function FakeMediaStream(tracks) {
  this.tracks = tracks;
}

const displayStream = () => {
  const video = {
    kind: 'video',
    contentHint: '',
    getSettings: () => ({ displaySurface: media.surface }),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  };
  return { getVideoTracks: () => [video], getAudioTracks: () => [{ enabled: true }], getTracks: () => [video] };
};
const micStream = () => ({ getVideoTracks: () => [], getAudioTracks: () => [{ enabled: true }], getTracks: () => [] });

const frames = () => socket.sent.filter((m) => typeof m === 'string').map((m) => JSON.parse(m));
const serverSays = (frame) => act(() => socket.options.onMessage({ data: JSON.stringify(frame) }));
const openSocket = () => {
  const target = { send: vi.fn((message) => socket.sent.push(message)) };
  act(() => socket.options.onOpen({ target }));
  return target;
};
const SETTINGS = { event: 'CONSENT_SETTINGS', available: true, mode: 'off', videoAllowed: true };
const videoJson = (ws) => ws.sent.filter((m) => typeof m === 'string').map((m) => JSON.parse(m));

/** The recorder hands over a chunk; the video connection opens with it. */
const deliverChunk = async () => {
  const [rec] = media.recorders;
  await act(async () => {
    rec.ondataavailable({ data: { size: 4, arrayBuffer: async () => new Uint8Array([0, 0, 0, 8]).buffer } });
  });
  await waitFor(() => expect(media.sockets).toHaveLength(1));
  const ws = media.sockets[0];
  act(() => ws.onopen());
  return ws;
};

const startStreaming = async () => {
  fireEvent.click(screen.getByRole('button', { name: 'Start Streaming' }));
  await waitFor(() => expect(frames().some((f) => f.callEvent === 'START')).toBe(true));
  return frames().find((f) => f.callEvent === 'START');
};

describe('StreamAudio screen video', () => {
  beforeEach(() => {
    socket.sent = [];
    media.recorders = [];
    media.sockets = [];
    media.surface = 'browser';
    vi.stubGlobal('AudioContext', FakeAudioContext);
    vi.stubGlobal('AudioWorkletNode', FakeWorkletNode);
    vi.stubGlobal('MediaRecorder', FakeMediaRecorder);
    vi.stubGlobal('MediaStream', FakeMediaStream);
    vi.stubGlobal('WebSocket', FakeWebSocket);
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    Object.defineProperty(window.navigator, 'mediaDevices', {
      configurable: true,
      value: {
        getDisplayMedia: vi.fn().mockImplementation(async () => displayStream()),
        getUserMedia: vi.fn().mockImplementation(async () => micStream()),
      },
    });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('offers the switch only when the transcriber allows video for this page', () => {
    render(<StreamAudio mode="stream" />);
    openSocket();
    serverSays({ ...SETTINGS, videoAllowed: false });
    expect(screen.queryByText('Also record screen video')).toBeNull();
    serverSays(SETTINGS);
    expect(screen.getByRole('checkbox', { name: /Also record screen video/ }).checked).toBe(false);
  });

  it('records the shared tab over its own connection, and Stop video keeps the audio going', async () => {
    render(<StreamAudio mode="stream" />);
    openSocket();
    serverSays(SETTINGS);
    fireEvent.click(screen.getByRole('checkbox', { name: /Also record screen video/ }));
    const start = await startStreaming();

    expect(window.navigator.mediaDevices.getDisplayMedia).toHaveBeenCalledWith(
      expect.objectContaining({ video: VIDEO_CONSTRAINTS, audio: true }),
    );
    await waitFor(() => expect(media.recorders).toHaveLength(1));
    expect(media.recorders[0].stream.tracks.map((t) => t.kind)).toEqual(['video']);
    expect(await screen.findByText('Screen video is ON')).toBeTruthy();
    expect(screen.getByText(/Recording a browser tab as video/)).toBeTruthy();
    expect(screen.getByTestId('screen-video-preview')).toBeTruthy();

    const ws = await deliverChunk();
    expect(ws.url).toContain('wss://transcriber.example/api/v1/ws?authorization=Bearer%20access');
    expect(videoJson(ws)[0]).toMatchObject({ callEvent: 'START_VIDEO', callId: start.callId });
    expect(ws.sent.filter((m) => typeof m !== 'string')).toHaveLength(1);

    fireEvent.click(screen.getByRole('button', { name: 'Stop video' }));
    await waitFor(() => expect(videoJson(ws).at(-1)).toEqual({ callEvent: 'END_VIDEO', callId: start.callId }));
    expect(await screen.findByText('Screen video stopped. Audio is still being streamed.')).toBeTruthy();
    expect(frames().some((f) => f.callEvent === 'END')).toBe(false);
    expect(screen.getByRole('button', { name: 'Stop Streaming' })).toBeTruthy();

    // The next meeting starts with the switch off again.
    fireEvent.click(screen.getByRole('button', { name: 'Stop Streaming' }));
    await waitFor(() => expect(frames().some((f) => f.callEvent === 'END')).toBe(true));
    await waitFor(() => expect(screen.getByRole('checkbox', { name: /Also record screen video/ }).checked).toBe(false));
  });

  it('warns when the whole screen is shared', async () => {
    media.surface = 'monitor';
    render(<StreamAudio mode="stream" />);
    openSocket();
    serverSays(SETTINGS);
    fireEvent.click(screen.getByRole('checkbox', { name: /Also record screen video/ }));
    await startStreaming();
    expect(await screen.findByText('Your whole screen is shared')).toBeTruthy();
    expect(screen.getByText(/Recording your whole screen as video/)).toBeTruthy();
  });

  it('tells the user when the transcriber refuses the video, and keeps streaming', async () => {
    render(<StreamAudio mode="stream" />);
    openSocket();
    serverSays(SETTINGS);
    fireEvent.click(screen.getByRole('checkbox', { name: /Also record screen video/ }));
    await startStreaming();
    await waitFor(() => expect(media.recorders).toHaveLength(1));
    const ws = await deliverChunk();
    act(() => ws.onclose({ code: 1008, reason: 'screen video is not enabled for this client' }));
    expect(await screen.findByText(/Screen video is not enabled on this deployment/)).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Stop Streaming' })).toBeTruthy();
  });

  it("ends the consent notice with the deployment's video sentence, and records it with the notice", async () => {
    render(<StreamAudio mode="stream" />);
    openSocket();
    serverSays({
      ...SETTINGS,
      mode: 'notify',
      rule: null,
      supported: true,
      noticeTemplate: 'Recorded.',
      videoNoticeText: 'Video too.',
    });
    expect(screen.getByTestId('consent-notice').textContent).toBe('Recorded.');
    fireEvent.click(screen.getByRole('checkbox', { name: /Also record screen video/ }));
    expect(screen.getByTestId('consent-notice').textContent).toBe('Recorded. Video too.');
    const start = await startStreaming();
    expect(start.consent.noticeText).toBe('Recorded. Video too.');
  });

  it('uses the shipped video sentence when the transcriber sends none', async () => {
    render(<StreamAudio mode="stream" />);
    openSocket();
    serverSays({ ...SETTINGS, mode: 'notify', rule: null, supported: true, noticeTemplate: 'Recorded.' });
    fireEvent.click(screen.getByRole('checkbox', { name: /Also record screen video/ }));
    expect(screen.getByTestId('consent-notice').textContent).toBe(`Recorded. ${DEFAULT_VIDEO_NOTICE_TEXT}`);
  });

  it('keeps the video switch as it is once the notice has been copied', async () => {
    render(<StreamAudio mode="stream" />);
    openSocket();
    serverSays({ ...SETTINGS, mode: 'notify', rule: null, supported: true, noticeTemplate: 'Recorded.' });
    const toggle = () => screen.getByRole('checkbox', { name: /Also record screen video/ });
    fireEvent.click(toggle());
    expect(toggle().disabled).toBe(false);
    fireEvent.click(screen.getByTestId('consent-notice-copy').querySelector('button'));
    expect(toggle().disabled).toBe(true);
    expect(toggle().checked).toBe(true);
    expect(screen.getByText(/Fixed once the notice is copied or confirmed/)).toBeTruthy();
    const start = await startStreaming();
    expect(start.consent.noticeText).toBe(`Recorded. ${DEFAULT_VIDEO_NOTICE_TEXT}`);
  });

  it('keeps the video switch as it is once the notice is confirmed as given', () => {
    render(<StreamAudio mode="stream" />);
    openSocket();
    serverSays({ ...SETTINGS, mode: 'notify', rule: null, supported: true, noticeTemplate: 'Recorded.' });
    fireEvent.click(screen.getByRole('checkbox', { name: /I have told everyone in the meeting/ }));
    const toggle = screen.getByRole('checkbox', { name: /Also record screen video/ });
    expect(toggle.disabled).toBe(true);
    expect(toggle.checked).toBe(false);
  });

  it('leaves the video switch free when the deployment shows no consent step', () => {
    render(<StreamAudio mode="stream" />);
    openSocket();
    serverSays(SETTINGS);
    expect(screen.getByRole('checkbox', { name: /Also record screen video/ }).disabled).toBe(false);
  });

  it("starts an opt-in meeting's video only once its consent is granted", async () => {
    render(<StreamAudio mode="stream" />);
    openSocket();
    serverSays({
      ...SETTINGS,
      mode: 'opt_in',
      rule: 'owner_attests',
      supported: true,
      noticeTemplate: 'May we record?',
    });
    fireEvent.click(screen.getByRole('checkbox', { name: /Also record screen video/ }));
    fireEvent.click(screen.getByRole('checkbox', { name: /Everyone in the meeting agreed/ }));
    fireEvent.click(screen.getByRole('radio', { name: /Out loud/ }));
    await startStreaming();
    expect(await screen.findByText(/Its video starts once the meeting's consent allows capture/)).toBeTruthy();
    expect(media.recorders).toHaveLength(0);
    serverSays({ event: 'CONSENT_STATE', mode: 'opt_in', status: 'granted', captureAllowed: true });
    await waitFor(() => expect(media.recorders).toHaveLength(1));
    expect(await screen.findByText('Screen video is ON')).toBeTruthy();
  });
});
