/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import fs from 'fs';
import path from 'path';
import { MEETING_TAB_UNCONFIRMED, NOT_THE_MEETING_TAB, VideoState, createPanelVideo, dataUrlBytes } from './panel-video';

type FakeSocket = {
  sent: unknown[],
  closed?: number,
  onopen?: () => void,
  onclose?: (event: { code: number, reason?: string }) => void,
  send: (data: unknown) => void,
  close: (code: number) => void,
  binaryType?: string,
};

const setup = (now = () => 1000) => {
  const sockets: FakeSocket[] = [];
  const recorder: string[] = [];
  const states: VideoState[] = [];
  const video = createPanelVideo({
    openSocket: async () => {
      const ws: FakeSocket = {
        sent: [],
        send(data) { this.sent.push(data); },
        close(code) { this.closed = code; },
      };
      sockets.push(ws);
      return ws as unknown as WebSocket;
    },
    sendToRecorder: (message) => recorder.push(message.action),
    onState: (state) => states.push(state),
    now,
  });
  return { video, sockets, recorder, states, last: () => states[states.length - 1] };
};

const chunk = (bytes: number[]) => `data:video/mp4;base64,${btoa(String.fromCharCode(...bytes))}`;
const flush = () => new Promise((resolve) => { setTimeout(resolve, 0); });
const json = (ws: FakeSocket) => ws.sent.filter((m) => typeof m === 'string').map((m) => JSON.parse(m as string));

describe('the panel\'s screen video', () => {
  test('decodes the recorder\'s chunks', () => {
    expect(Array.from(dataUrlBytes(chunk([0, 1, 255])))).toEqual([0, 1, 255]);
  });

  test('starts the recorder once START is sent, and streams its chunks on a connection of their own', async () => {
    let clock = 1000;
    const h = setup(() => clock);
    h.video.begin('Weekly - 2026', true, false);
    expect(h.recorder).toEqual(['StartVideo']);
    expect(h.last().status).toBe('waiting');
    clock = 1750;
    h.video.onRecorderMessage({ action: 'VideoStarted' });
    expect(h.last().status).toBe('on');
    h.video.onRecorderMessage({ action: 'VideoChunk', data: chunk([0, 0, 0, 8]) });
    await flush();
    const ws = h.sockets[0];
    ws.onopen?.();
    expect(json(ws)[0]).toEqual({ callEvent: 'START_VIDEO', callId: 'Weekly - 2026', videoTimeOffsetMs: 750 });
    expect(ws.sent.filter((m) => typeof m !== 'string')).toHaveLength(1);

    h.video.stop();
    expect(h.recorder).toEqual(['StartVideo', 'StopVideo']);
    h.video.onRecorderMessage({ action: 'VideoStopped', reason: 'stopped' });
    expect(json(ws)[json(ws).length - 1]).toEqual({ callEvent: 'END_VIDEO', callId: 'Weekly - 2026' });
    ws.onclose?.({ code: 1000 });
    expect(h.last().status).toBe('stopped');
  });

  test('waits for an opt-in meeting\'s consent to be granted', () => {
    const h = setup();
    h.video.begin('m', true, true);
    expect(h.recorder).toEqual([]);
    h.video.granted();
    expect(h.recorder).toEqual(['StartVideo']);
  });

  test('a consent granted in notify, after the video has started, does not start it again', () => {
    const h = setup();
    h.video.begin('m', true, false);
    h.video.granted();
    expect(h.recorder).toEqual(['StartVideo']);
  });

  test('records nothing when the switch is off', () => {
    const h = setup();
    h.video.begin('m', false, false);
    h.video.onRecorderMessage({ action: 'VideoStarted' });
    expect(h.recorder).toEqual([]);
    expect(h.sockets).toHaveLength(0);
  });

  test('says so when the shared surface is not the meeting tab, and the audio goes on', () => {
    const h = setup();
    h.video.begin('m', true, false);
    h.video.onRecorderMessage({ action: 'VideoUnavailable', reason: 'not-meeting-tab' });
    expect(h.last()).toMatchObject({ status: 'failed', message: NOT_THE_MEETING_TAB });
    expect(h.sockets).toHaveLength(0);
  });

  test('says so when the browser cannot confirm the meeting tab was shared', () => {
    const h = setup();
    h.video.begin('m', true, false);
    h.video.onRecorderMessage({ action: 'VideoUnavailable', reason: 'unverified' });
    expect(h.last()).toMatchObject({ status: 'failed', message: MEETING_TAB_UNCONFIRMED });
  });

  test('names the meeting tab in both messages, and only the meeting tab', () => {
    expect(NOT_THE_MEETING_TAB).toContain('only from the meeting tab');
    expect(MEETING_TAB_UNCONFIRMED).toContain('only from the meeting tab');
  });

  test('stops the recorder when the transcriber refuses the video', async () => {
    const h = setup();
    h.video.begin('m', true, false);
    h.video.onRecorderMessage({ action: 'VideoStarted' });
    h.video.onRecorderMessage({ action: 'VideoChunk', data: chunk([1]) });
    await flush();
    h.sockets[0].onopen?.();
    h.sockets[0].onclose?.({ code: 1008, reason: 'screen video is not enabled for this client' });
    expect(h.recorder).toEqual(['StartVideo', 'StopVideo']);
    expect(h.last().status).toBe('failed');
    expect(h.last().message).toMatch(/not enabled/);
  });

  test('sends the recorder\'s last chunk before ending the stream when the capture ends', async () => {
    const h = setup();
    h.video.begin('m', true, false);
    h.video.onRecorderMessage({ action: 'VideoStarted' });
    h.video.onRecorderMessage({ action: 'VideoChunk', data: chunk([1]) });
    await flush();
    const ws = h.sockets[0];
    ws.onopen?.();
    h.video.end();
    expect(h.recorder).toEqual(['StartVideo', 'StopVideo']);
    h.video.onRecorderMessage({ action: 'VideoChunk', data: chunk([2]) });
    h.video.onRecorderMessage({ action: 'VideoStopped', reason: 'stopped' });
    expect(ws.sent.filter((m) => typeof m !== 'string')).toHaveLength(2);
    expect(json(ws)[json(ws).length - 1]).toEqual({ callEvent: 'END_VIDEO', callId: 'm' });
    expect(h.last().status).toBe('off');
  });

  test('stops the recorder when Stop video comes while the video is still starting', () => {
    const h = setup();
    h.video.begin('m', true, false);
    h.video.stop();
    expect(h.recorder).toEqual(['StartVideo', 'StopVideo']);
    expect(h.last().status).toBe('stopped');
  });

  test('a new capture ends a previous one still waiting for its last chunk', async () => {
    const h = setup();
    h.video.begin('first', true, false);
    h.video.onRecorderMessage({ action: 'VideoStarted' });
    h.video.onRecorderMessage({ action: 'VideoChunk', data: chunk([1]) });
    await flush();
    h.sockets[0].onopen?.();
    h.video.end();
    h.video.begin('second', true, false);
    expect(json(h.sockets[0])[json(h.sockets[0]).length - 1]).toEqual({ callEvent: 'END_VIDEO', callId: 'first' });
    expect(h.last().status).toBe('waiting');
  });

  test('goes back to off when the capture ends', () => {
    const h = setup();
    h.video.begin('m', true, false);
    h.video.end();
    expect(h.last().status).toBe('off');
  });
});

test('the recorder records with the values screen-video.js gives', () => {
  // recorder.js is a plain script in the meeting tab, so it keeps its own copy.
  const recorder = fs.readFileSync(path.join(__dirname, '../../public/content_scripts/recorder/recorder.js'), 'utf8');
  // eslint-disable-next-line @typescript-eslint/no-var-requires
  const sv = require('./screen-video');
  expect(recorder).toContain(`const VIDEO_MIME_TYPES = ${JSON.stringify(sv.VIDEO_MIME_TYPES).replace(/"/g, "'").replace(/,/g, ', ')};`);
  expect(recorder).toContain(`const VIDEO_TIMESLICE_MS = ${sv.TIMESLICE_MS};`);
  expect(recorder).toContain('const VIDEO_RECORDER_OPTIONS = { videoKeyFrameIntervalDuration: 1800, videoBitsPerSecond: 1500000 };');
  expect(sv.RECORDER_OPTIONS).toEqual({ videoKeyFrameIntervalDuration: 1800, videoBitsPerSecond: 1500000 });
  expect(recorder).toContain('const VIDEO_CONSTRAINTS = { frameRate: { ideal: 5, max: 5 }, width: { max: 1920 }, height: { max: 1920 } };');
  expect(sv.VIDEO_CONSTRAINTS).toEqual({ frameRate: { ideal: 5, max: 5 }, width: { max: 1920 }, height: { max: 1920 } });
});

test('screen-video.js is the same file as the Stream Audio/Video page\'s', () => {
  const page = path.resolve(__dirname, '../../../lma-ai-stack/source/ui/src/components/stream-audio/screen-video.js');
  if (!fs.existsSync(page)) return; // not in a full checkout
  expect(fs.readFileSync(path.join(__dirname, 'screen-video.js'), 'utf8')).toBe(fs.readFileSync(page, 'utf8'));
});
