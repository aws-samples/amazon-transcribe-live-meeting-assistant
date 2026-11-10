/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The Stream Audio page's consent step, with the transcriber's socket faked.
 *
 * - The page asks for the consent settings when its socket opens, and shows the
 *   step only when the transcriber answers with a mode other than off.
 * - Opt-in: streaming does not start until everyone's agreement is confirmed
 *   with a method; then START carries the confirmation.
 * - Notify: streaming may start unconfirmed, and Participant objected stops it,
 *   telling the transcriber, with no END of its own.
 * - A CONSENT_STATE with captureAllowed false stops the page's capture.
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

const node = () => ({ connect: vi.fn(), disconnect: vi.fn() });
const fakeStream = () => ({ getVideoTracks: () => [], getAudioTracks: () => [{ enabled: true }], getTracks: () => [] });

// Constructed with `new` by the page: a function constructor that returns an
// object yields that object.
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

const OPT_IN = {
  event: 'CONSENT_SETTINGS',
  available: true,
  mode: 'opt_in',
  rule: 'owner_attests',
  supported: true,
  noticeTemplate: '{LMA_USER} would like to record {meeting}. Reply "{consent_phrase}" to agree.',
  consentPhrase: 'I consent',
  objectionPhrase: 'I object',
};

const frames = () => socket.sent.filter((m) => typeof m === 'string').map((m) => JSON.parse(m));
const serverSays = (frame) => act(() => socket.options.onMessage({ data: JSON.stringify(frame) }));

// What the page sends on the open socket is recorded with the rest.
const openSocket = () => {
  const target = { send: vi.fn((message) => socket.sent.push(message)) };
  act(() => socket.options.onOpen({ target }));
  return target;
};

describe('StreamAudio consent step', () => {
  beforeEach(() => {
    socket.sent = [];
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

  it('sends START with consent mode off when the settings are not known yet', async () => {
    render(<StreamAudio mode="stream" />);
    openSocket();
    fireEvent.click(screen.getByRole('button', { name: 'Start Streaming' }));
    await waitFor(() => expect(frames().some((f) => f.callEvent === 'START')).toBe(true));
    expect(frames().find((f) => f.callEvent === 'START').consent).toMatchObject({ mode: 'off', confirmed: false });
  });

  it('asks for the settings on open and shows no step while consent is off', () => {
    render(<StreamAudio mode="stream" />);
    const target = openSocket();
    expect(JSON.parse(target.send.mock.calls[0][0])).toEqual({ callEvent: 'CONSENT_SETTINGS' });
    serverSays({ ...OPT_IN, mode: 'off', noticeTemplate: '' });
    expect(screen.queryByText('Meeting consent')).toBeNull();
  });

  it('holds an opt-in meeting until it is confirmed, then sends the confirmation with START', async () => {
    render(<StreamAudio mode="stream" />);
    openSocket();
    serverSays(OPT_IN);
    expect(screen.getByTestId('consent-notice').textContent).toBe(
      'ana would like to record Stream Audio. Reply "I consent" to agree.',
    );

    fireEvent.click(screen.getByRole('button', { name: 'Start Streaming' }));
    expect(await screen.findByText('Confirm that everyone agreed before you start.')).toBeTruthy();
    expect(frames().some((f) => f.callEvent === 'START')).toBe(false);

    fireEvent.click(screen.getByRole('checkbox', { name: /Everyone in the meeting agreed/ }));
    fireEvent.click(screen.getByRole('radio', { name: /Out loud/ }));
    fireEvent.click(screen.getByRole('button', { name: 'Start Streaming' }));

    await waitFor(() => expect(frames().some((f) => f.callEvent === 'START')).toBe(true));
    const start = frames().find((f) => f.callEvent === 'START');
    expect(start.consent).toEqual({
      mode: 'opt_in',
      confirmed: true,
      method: 'verbal',
      noticeText: 'ana would like to record Stream Audio. Reply "I consent" to agree.',
      lmaUser: 'ana',
      meetingName: 'Stream Audio',
      client: 'Stream Audio',
    });

    // The transcriber could not record the consent: the page stops, sending no END.
    serverSays({
      event: 'CONSENT_STATE',
      captureAllowed: false,
      code: 'CONSENT_NOT_RECORDED',
      message: 'Not recorded.',
    });
    expect(await screen.findByRole('button', { name: 'Start Streaming' })).toBeTruthy();
    expect(screen.getByText('Not recorded.')).toBeTruthy();
    expect(frames().some((f) => f.callEvent === 'END')).toBe(false);
    // A meeting was created, so it can be opened; the next one is confirmed afresh.
    expect(screen.getByText('Open recorded meeting')).toBeTruthy();
    await waitFor(() =>
      expect(screen.getByRole('checkbox', { name: /Everyone in the meeting agreed/ }).checked).toBe(false),
    );
  });

  it('offers no meeting to open after the transcriber refuses the START', async () => {
    render(<StreamAudio mode="stream" />);
    openSocket();
    serverSays({ ...OPT_IN, mode: 'notify', rule: null, noticeTemplate: 'Recorded.' });
    fireEvent.click(screen.getByRole('button', { name: 'Start Streaming' }));
    await waitFor(() => expect(frames().some((f) => f.callEvent === 'START')).toBe(true));
    serverSays({
      event: 'CONSENT_STATE',
      captureAllowed: false,
      code: 'CONSENT_REQUIRED',
      message: 'Update the client.',
    });
    expect(await screen.findByRole('button', { name: 'Start Streaming' })).toBeTruthy();
    expect(screen.getByText('Update the client.')).toBeTruthy();
    await waitFor(() => expect(screen.queryByText('Open recorded meeting')).toBeNull());
  });

  it('lets a notify meeting start unconfirmed, and stops it when a participant objects', async () => {
    render(<StreamAudio mode="stream" />);
    openSocket();
    serverSays({ ...OPT_IN, mode: 'notify', rule: null, noticeTemplate: 'Recorded. Say "{objection_phrase}".' });

    fireEvent.click(screen.getByRole('button', { name: 'Start Streaming' }));
    await waitFor(() => expect(frames().some((f) => f.callEvent === 'START')).toBe(true));
    expect(frames().find((f) => f.callEvent === 'START').consent).toMatchObject({ mode: 'notify', confirmed: false });

    fireEvent.change(screen.getByLabelText('Name of the participant who objected'), { target: { value: 'Bo Chen' } });
    fireEvent.click(await screen.findByRole('button', { name: 'Participant objected' }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Start Streaming' })).toBeTruthy());
    expect(frames().find((f) => f.callEvent === 'CONSENT_OBJECTION')).toEqual({
      callEvent: 'CONSENT_OBJECTION',
      participantName: 'Bo Chen',
      source: 'voice',
    });
    expect(frames().some((f) => f.callEvent === 'END')).toBe(false);
  });
});
