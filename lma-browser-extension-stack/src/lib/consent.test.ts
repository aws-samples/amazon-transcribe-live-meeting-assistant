/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import {
  ConsentSettings,
  captureStopped,
  consentPayload,
  consentRequired,
  describeConsentState,
  emptyStep,
  fetchConsentSettings,
  parseServerFrame,
  renderNotice,
  startBlockedReason,
  transcriberUrl,
  withoutConsent,
} from './consent';

const OPT_IN: ConsentSettings = {
  event: 'CONSENT_SETTINGS',
  available: true,
  mode: 'opt_in',
  rule: 'owner_attests',
  supported: true,
  noticeTemplate: '{LMA_USER} would like to record {meeting}. Reply "{consent_phrase}" or "{objection_phrase}".',
  consentPhrase: 'I consent',
  objectionPhrase: 'I object',
};
const NOTIFY: ConsentSettings = { ...OPT_IN, mode: 'notify', rule: null, noticeTemplate: 'Recorded.' };

describe('the consent step', () => {
  test('fills the notice as the transcriber does', () => {
    expect(renderNotice(OPT_IN.noticeTemplate, { lmaUser: 'Ana', meeting: 'Weekly', consentPhrase: 'I consent', objectionPhrase: 'I object' }))
      .toBe('Ana would like to record Weekly. Reply "I consent" or "I object".');
    expect(renderNotice('{LMA_USER} / {meeting}', {})).toBe('The meeting organizer / this meeting');
    expect(renderNotice('x'.repeat(3000), {})).toHaveLength(2000);
  });

  test('applies only when the transcriber answered with notify or opt-in', () => {
    expect(consentRequired(null)).toBe(false);
    expect(consentRequired({ ...OPT_IN, mode: 'off' })).toBe(false);
    expect(consentRequired({ event: 'CONSENT_SETTINGS', available: false })).toBe(false);
    expect(consentRequired(NOTIFY)).toBe(true);
  });

  test('holds an opt-in start until it is confirmed with a method', () => {
    expect(startBlockedReason(OPT_IN, emptyStep())).toMatch(/Confirm that everyone agreed/);
    expect(startBlockedReason(OPT_IN, { ...emptyStep(), confirmed: true })).toMatch(/Choose how everyone agreed/);
    expect(startBlockedReason(OPT_IN, { ...emptyStep(), confirmed: true, method: 'verbal' })).toBe('');
    expect(startBlockedReason({ ...OPT_IN, supported: false, reason: 'Use a Virtual Participant.' }, emptyStep()))
      .toBe('Use a Virtual Participant.');
  });

  test('lets a notify meeting start unconfirmed', () => {
    expect(startBlockedReason(NOTIFY, emptyStep())).toBe('');
    expect(startBlockedReason(null, emptyStep())).toBe('');
  });

  test('counts a notice posted in the chat only once the user has seen it appear', () => {
    expect(startBlockedReason(NOTIFY, { ...emptyStep(), chatPost: 'sent' })).toMatch(/appeared in the meeting chat/);
    expect(startBlockedReason(NOTIFY, { ...emptyStep(), chatPost: 'confirmed' })).toBe('');
    expect(startBlockedReason(NOTIFY, { ...emptyStep(), chatPost: 'failed' })).toBe('');
  });

  test('sends the step with START, and mode off when the settings are not known', () => {
    const step = { confirmed: true, method: 'chat' as const, statement: '  We all agreed. ', chatPost: 'confirmed' as const };
    expect(consentPayload(OPT_IN, step, { lmaUser: 'Ana', meetingName: 'Weekly', noticeText: 'N' })).toEqual({
      mode: 'opt_in',
      confirmed: true,
      method: 'chat',
      noticeText: 'N',
      lmaUser: 'Ana',
      meetingName: 'Weekly',
      statement: 'We all agreed.',
      client: 'Browser extension',
    });
    expect(consentPayload(null, step, { lmaUser: 'Ana', meetingName: 'Weekly', noticeText: '' }))
      .toEqual({ mode: 'off', confirmed: false, client: 'Browser extension' });
  });

  test('reads the transcriber frames', () => {
    expect(parseServerFrame('{"event":"CONSENT_STATE","captureAllowed":false}')).toEqual({ event: 'CONSENT_STATE', captureAllowed: false });
    expect(parseServerFrame('not json')).toBeNull();
    expect(parseServerFrame(new ArrayBuffer(2))).toBeNull();
    expect(captureStopped({ event: 'CONSENT_STATE', captureAllowed: false })).toBe(true);
    expect(captureStopped({ event: 'CONSENT_STATE', captureAllowed: true })).toBe(false);
    expect(describeConsentState({ event: 'CONSENT_STATE', status: 'granted', captureAllowed: true })?.type).toBe('success');
    expect(describeConsentState({ event: 'CONSENT_STATE', status: 'objected', captureAllowed: false })?.text).toMatch(/objected/);
    expect(describeConsentState({ event: 'CONSENT_STATE', captureAllowed: false, message: 'Refused.' })).toEqual({ type: 'error', text: 'Refused.' });
  });

  test('leaves the consent step out of the frames after START', () => {
    const start = { callEvent: 'START', callId: 'c', consent: { mode: 'notify' } };
    expect(withoutConsent({ ...start, callEvent: 'END' })).toEqual({ callEvent: 'END', callId: 'c' });
    const speaker: { callEvent: string, callId: string, consent?: unknown } = { callEvent: 'SPEAKER_CHANGE', callId: 'c' };
    expect(withoutConsent(speaker)).toEqual({ callEvent: 'SPEAKER_CHANGE', callId: 'c' });
  });

  test('builds the upgrade URL with the tokens in the query string', () => {
    expect(transcriberUrl('wss://x/api/v1/ws', { access_token: 'a b', id_token: 'i', refresh_token: 'r' }))
      .toBe('wss://x/api/v1/ws?authorization=Bearer%20a%20b&id_token=i&refresh_token=r');
  });
});

describe('fetchConsentSettings', () => {
  class FakeSocket {
    static last: FakeSocket;
    sent: string[] = [];
    closed = false;
    onopen: (() => void) | null = null;
    onmessage: ((event: { data: string }) => void) | null = null;
    onerror: (() => void) | null = null;
    onclose: (() => void) | null = null;
    constructor(public url: string) { FakeSocket.last = this; }
    send(data: string) { this.sent.push(data); }
    close() { this.closed = true; }
  }

  test('asks for the settings naming this client, and closes the connection', async () => {
    const answer = fetchConsentSettings('wss://x', { WebSocketClass: FakeSocket as unknown as typeof WebSocket });
    const socket = FakeSocket.last;
    socket.onopen?.();
    expect(JSON.parse(socket.sent[0])).toEqual({ callEvent: 'CONSENT_SETTINGS', clientType: 'BROWSER_EXTENSION' });
    socket.onmessage?.({ data: JSON.stringify({ ...NOTIFY, videoAllowed: true }) });
    await expect(answer).resolves.toMatchObject({ mode: 'notify', videoAllowed: true });
    expect(socket.closed).toBe(true);
  });

  test('gives null when the transcriber does not answer', async () => {
    jest.useFakeTimers();
    const answer = fetchConsentSettings('wss://x', { timeoutMs: 50, WebSocketClass: FakeSocket as unknown as typeof WebSocket });
    jest.advanceTimersByTime(60);
    await expect(answer).resolves.toBeNull();
    jest.useRealTimers();
  });
});
