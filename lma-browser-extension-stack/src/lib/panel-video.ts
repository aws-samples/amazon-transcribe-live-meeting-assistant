/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The side panel's half of the extension's screen video.
 *
 * The recorder in the meeting tab (public/content_scripts/recorder/recorder.js)
 * records the tab with MediaRecorder once it is told to ("StartVideo") and
 * hands each chunk to the panel ("VideoChunk"). The panel sends the chunks to
 * the transcriber on a video connection of their own (createVideoUplink in
 * screen-video.js, shared with the Stream Audio/Video page), and reads the key
 * frame spacing from them (createKeyframeMonitor).
 *
 * The video starts once the meeting's consent allows capture: when START has
 * been sent, or in opt-in when the transcriber has granted the consent. Its
 * offset from the audio is measured from that moment, which is where the
 * transcript's clock starts.
 */
import * as screenVideo from './screen-video';

// screen-video.js is plain JavaScript shared with the Stream Audio/Video page;
// its option objects carry defaults TypeScript would read as their only types.
/* eslint-disable @typescript-eslint/no-explicit-any */
const { createKeyframeMonitor, createVideoUplink, videoStopMessage } = screenVideo as any;
type Uplink = { send: (chunk: ArrayBuffer) => void, stop: () => void };
type Monitor = { push: (bytes: Uint8Array) => void };

export type VideoStatus = 'off' | 'waiting' | 'on' | 'stopped' | 'failed';
export type VideoState = { status: VideoStatus, message: string, slowGap: number | null };
export const VIDEO_OFF: VideoState = { status: 'off', message: '', slowGap: null };

/** What the panel says when the shared surface is not the meeting tab. */
export const NOT_THE_MEETING_TAB =
  'Screen video is recorded only from the meeting tab itself, and something else was shared, so this meeting '
  + 'records audio only.';
const UNSUPPORTED = 'This browser cannot record screen video in the format LMA needs, so this meeting records audio only.';

type Options = {
  openSocket: () => Promise<WebSocket>,
  /** Sends a message to the recorder in the meeting tab. */
  sendToRecorder: (message: { action: string }) => void,
  onState: (state: VideoState) => void,
  log?: (message: string) => void,
  now?: () => number,
  uplinkOptions?: Record<string, unknown>,
  /** How long the end of a capture waits for the recorder's last chunk. */
  endWaitMs?: number,
  setTimer?: (fn: () => void, ms: number) => ReturnType<typeof setTimeout>,
  clearTimer?: (timer: ReturnType<typeof setTimeout>) => void,
};

/** Decodes the recorder's base64 data URL into bytes. */
export const dataUrlBytes = (dataUrl: string): Uint8Array => {
  const comma = dataUrl.indexOf(',');
  const binary = atob(comma >= 0 ? dataUrl.slice(comma + 1) : dataUrl);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
  return bytes;
};

export const createPanelVideo = ({
  openSocket, sendToRecorder, onState, log = () => undefined, now = () => Date.now(), uplinkOptions = {},
  endWaitMs = 3000, setTimer = (fn: () => void, ms: number) => setTimeout(fn, ms), clearTimer = (t: ReturnType<typeof setTimeout>) => clearTimeout(t),
}: Options) => {
  let endTimer: ReturnType<typeof setTimeout> | null = null;
  let awaitingGrant = false;
  let state: VideoState = VIDEO_OFF;
  let callId: string | null = null;
  let since = 0;
  let wanted = false;
  let uplink: Uplink | null = null;
  let monitor: Monitor | null = null;

  const set = (change: Partial<VideoState>) => {
    state = { ...state, ...change };
    onState(state);
  };

  const fail = (message: string) => {
    if (state.status === 'off') return;
    set({ status: 'failed', message });
  };

  /** START has been sent for `meeting`; `waitForGrant` in opt-in. */
  const begin = (meeting: string, wantVideo: boolean, waitForGrant: boolean) => {
    // A previous capture still waiting for its last chunk is ended now, so its
    // timer cannot end this one's video.
    if (endTimer !== null) finishEnd();
    uplink = null;
    monitor = null;
    callId = meeting;
    wanted = wantVideo;
    state = VIDEO_OFF;
    if (!wantVideo) return;
    set({ status: 'waiting' });
    awaitingGrant = waitForGrant;
    if (!waitForGrant) {
      since = now();
      sendToRecorder({ action: 'StartVideo' });
    }
  };

  /** The transcriber granted the meeting's consent (opt-in). */
  const granted = () => {
    // Only for a capture told to wait for it: in notify the transcriber reports
    // the consent granted too, after the video has already been started.
    if (!awaitingGrant || !wanted || state.status !== 'waiting' || uplink) return;
    awaitingGrant = false;
    since = now();
    sendToRecorder({ action: 'StartVideo' });
  };

  const onRecorderMessage = (request: { action: string, data?: string, reason?: string }) => {
    if (!wanted || callId === null) return;
    if (request.action === 'VideoUnavailable') {
      wanted = false;
      fail(request.reason === 'surface' ? NOT_THE_MEETING_TAB : UNSUPPORTED);
    } else if (request.action === 'VideoStarted') {
      monitor = createKeyframeMonitor({ onSlowKeyframes: (gap: number) => set({ slowGap: gap }) });
      uplink = createVideoUplink({
        openSocket,
        callId,
        offsetMs: Math.max(0, Math.round(now() - since)),
        log,
        ...uplinkOptions,
        onStopped: (reason: string) => {
          uplink = null;
          if (reason === 'stopped') {
            if (state.status === 'on') set({ status: 'stopped', message: '' });
            return;
          }
          // The transcriber refused the video, or its connection was lost: the
          // recorder stops, and the audio carries on.
          sendToRecorder({ action: 'StopVideo' });
          fail(videoStopMessage(reason));
        },
      });
      set({ status: 'on' });
    } else if (request.action === 'VideoChunk' && request.data) {
      const bytes = dataUrlBytes(request.data);
      monitor?.push(bytes);
      uplink?.send(bytes.buffer);
    } else if (request.action === 'VideoStopped') {
      if (endTimer !== null) {
        finishEnd();
        return;
      }
      if (request.reason === 'ended') fail(videoStopMessage('ended'));
      uplink?.stop();
    }
  };

  /** The user's Stop video: the recorder stops, and its last chunk ends the stream. */
  const stop = () => {
    if (state.status === 'waiting' && !uplink) {
      // The recorder may already have been asked to start.
      sendToRecorder({ action: 'StopVideo' });
      wanted = false;
      set({ status: 'stopped', message: '' });
      return;
    }
    sendToRecorder({ action: 'StopVideo' });
  };

  /** The capture ended: whatever is still open is ended, and the switch goes back to off. */
  const finishEnd = () => {
    if (endTimer !== null) clearTimer(endTimer);
    endTimer = null;
    wanted = false;
    uplink?.stop();
    uplink = null;
    state = VIDEO_OFF;
    onState(state);
  };

  /**
   * The capture ended: the recorder's last chunk is sent before the stream is
   * ended (it comes with "VideoStopped"), waiting at most `endWaitMs` for it;
   * then the switch goes back to off.
   */
  const end = () => {
    if (!uplink) {
      finishEnd();
      return;
    }
    if (endTimer !== null) return;
    sendToRecorder({ action: 'StopVideo' });
    endTimer = setTimer(finishEnd, endWaitMs);
  };

  return { begin, granted, onRecorderMessage, stop, end, state: () => state };
};
