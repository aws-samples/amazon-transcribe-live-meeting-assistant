/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
// The provider's wiring of the consent step to the transcriber: what START,
// SPEAKER_CHANGE, END and CONSENT_OBJECTION carry, and when no END is sent.
// The socket (react-use-websocket) and Chrome's extension APIs are fakes.
import React from 'react';
import { act, render, waitFor } from '@testing-library/react';
import IntegrationProvider, { useIntegration } from './ProviderIntegrationContext';

const socket: { sent: any[], options: any } = { sent: [], options: null };

jest.mock('react-use-websocket', () => {
  const actual = jest.requireActual('react-use-websocket');
  return {
    __esModule: true,
    ...actual,
    default: (_url: string, options: any) => {
      socket.options = options;
      return {
        sendMessage: (message: any) => socket.sent.push(message),
        readyState: actual.ReadyState.OPEN,
        getWebSocket: () => ({ close: () => undefined }),
      };
    },
  };
});

jest.mock('./SettingsContext', () => ({
  useSettings: () => ({
    wssEndpoint: 'wss://transcriber.example/api/v1/ws',
    graphqlEndpoint: 'https://api.example/graphql',
    recordingMessage: 'LMA is recording.',
    stopRecordingMessage: 'LMA stopped recording.',
  }),
}));

const USER = { access_token: 'access', id_token: 'id', refresh_token: 'refresh' };
jest.mock('./UserContext', () => ({
  useUserContext: () => ({ user: USER, checkTokenExpired: async () => false, login: () => undefined }),
}));

type Listener = (request: any, sender: any, sendResponse: any) => unknown;
let listeners: Listener[] = [];
const runtimeMessages: any[] = [];

beforeEach(() => {
  socket.sent = [];
  socket.options = null;
  listeners = [];
  runtimeMessages.length = 0;
  (global as any).chrome = {
    runtime: {
      sendMessage: (message: any) => { runtimeMessages.push(message); },
      onMessage: {
        addListener: (listener: Listener) => { listeners.push(listener); },
        removeListener: (listener: Listener) => { listeners = listeners.filter((l) => l !== listener); },
      },
    },
    tabs: {
      query: async () => [{ id: 7, url: 'https://zoom.us/wc/1' }],
      sendMessage: async () => ({}),
      onActivated: { addListener: () => undefined, removeListener: () => undefined },
    },
  };
  window.alert = jest.fn();
});

const frames = () => socket.sent.filter((m) => typeof m === 'string').map((m) => JSON.parse(m));
const ofEvent = (event: string) => frames().filter((f) => f.callEvent === event);

// The recorder in the captured tab reports its sampling rate once capture runs;
// the provider sends START then.
const fromRecorder = async (request: any) => {
  await act(async () => {
    await Promise.all(listeners.map((listener) => listener(request, { tab: { id: 7, url: 'https://zoom.us/wc/1' } }, () => undefined)));
  });
};

let integration: ReturnType<typeof useIntegration>;
const Probe = () => {
  integration = useIntegration();
  return null;
};

const renderProvider = () => render(<IntegrationProvider><Probe /></IntegrationProvider>);

const NOTIFY_CONSENT = { mode: 'notify', noticeText: 'This meeting is recorded.', noticeConfirmed: true };

const startCapture = async (consent?: Record<string, unknown>) => {
  await act(async () => {
    await integration.startTranscription(USER, 'Ana', 'Weekly', undefined, consent, false);
  });
  await fromRecorder({ action: 'SamplingRate', samplingRate: 16000 });
  await waitFor(() => expect(integration.isTranscribing).toBe(true));
};

test('START carries the consent step the panel gave', async () => {
  renderProvider();
  await startCapture(NOTIFY_CONSENT);
  const [start] = ofEvent('START');
  expect(start).toMatchObject({ clientType: 'BROWSER_EXTENSION', localSpeakerName: 'Ana', samplingRate: 16000 });
  expect(start.consent).toEqual(NOTIFY_CONSENT);
});

test('START without a consent step sends mode off, so an opt-in deployment can refuse it', async () => {
  renderProvider();
  await startCapture(undefined);
  const [start] = ofEvent('START');
  expect(start.consent).toMatchObject({ mode: 'off' });
});

test('SPEAKER_CHANGE and END leave the consent step out', async () => {
  renderProvider();
  await startCapture(NOTIFY_CONSENT);
  await fromRecorder({ action: 'ActiveSpeakerChange', active_speaker: 'Bo' });
  const [change] = ofEvent('SPEAKER_CHANGE');
  expect(change.activeSpeaker).toBe('Bo');
  expect(change).not.toHaveProperty('consent');
  act(() => integration.stopTranscription());
  const ends = ofEvent('END');
  expect(ends).toHaveLength(1);
  expect(ends[0]).not.toHaveProperty('consent');
});

test('Participant objected sends CONSENT_OBJECTION and no END', async () => {
  renderProvider();
  await startCapture(NOTIFY_CONSENT);
  act(() => integration.reportObjection('Bo'));
  expect(ofEvent('CONSENT_OBJECTION')).toEqual([{ callEvent: 'CONSENT_OBJECTION', participantName: 'Bo', source: 'voice' }]);
  expect(ofEvent('END')).toHaveLength(0);
  expect(runtimeMessages).toContainEqual({ action: 'StopTranscription' });
  await waitFor(() => expect(integration.isTranscribing).toBe(false));
  expect(integration.consentFrame).toMatchObject({ status: 'objected', captureAllowed: false });
});

test('no END is sent after the transcriber stops capture for the meeting\'s consent', async () => {
  renderProvider();
  await startCapture(NOTIFY_CONSENT);
  act(() => {
    socket.options.onMessage({ data: JSON.stringify({ event: 'CONSENT_STATE', mode: 'notify', status: 'objected', captureAllowed: false }) });
  });
  await waitFor(() => expect(integration.consentFrame).toMatchObject({ captureAllowed: false }));
  // The transcriber closes the connection next.
  act(() => socket.options.onClose({ code: 1008 }));
  await waitFor(() => expect(integration.isTranscribing).toBe(false));
  expect(ofEvent('END')).toHaveLength(0);
});

test('a capture that ends normally sends END once', async () => {
  renderProvider();
  await startCapture(NOTIFY_CONSENT);
  act(() => {
    socket.options.onMessage({ data: JSON.stringify({ event: 'CONSENT_STATE', mode: 'notify', status: 'notified', captureAllowed: true }) });
  });
  act(() => integration.stopTranscription());
  await waitFor(() => expect(integration.isTranscribing).toBe(false));
  expect(ofEvent('END')).toHaveLength(1);
});
