/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * One meeting's stream to the WebSocket transcriber, kept going across the
 * page's reconnects (docs/websocket-streaming-api.md#reconnection).
 *
 * The transcriber starts a meeting on START and drops audio that arrives on a
 * connection that has had no START. When a connection closes without END it
 * holds the meeting for 60 seconds, and a START for the same callId from the
 * same user on a new connection continues it. So on every connection that
 * opens while a meeting is in progress this sends the meeting's START again,
 * unchanged (same callId and consent step), before anything else; then the
 * control frames and audio that waited for it; then an END the user asked for
 * while disconnected.
 *
 * Audio captured while disconnected is held up to `bufferSeconds` and sent
 * after the START, the oldest dropped first beyond that, as the Desktop Capture
 * App does. A connection lost for longer than `resumeWindowMs` ends the stream
 * here and calls `onResumeExpired`: by then the transcriber has ended the
 * meeting, and a START would start a second session of it rather than continue
 * it.
 *
 * Framework-free so it can be tested without React; use-transcriber-socket.js
 * wires it to the page's socket.
 */

// Shorter than the transcriber's 60 s hold (AUDIO_RECONNECT_GRACE_MS), because
// the two ends do not notice a lost connection at the same moment.
export const RESUME_WINDOW_MS = 55 * 1000;
// What the Desktop Capture App holds through an outage.
export const RECONNECT_BUFFER_SECONDS = 3;

// The capture worklet sends interleaved stereo 16-bit PCM.
const CHANNELS = 2;
const BYTES_PER_SAMPLE = 2;
const DEFAULT_SAMPLING_RATE = 48000;
const WS_OPEN = 1;

export const CONNECTION_LOST_MESSAGE =
  'The connection to LMA was lost and could not be restored within a minute, so the meeting was ended. ' +
  'Everything captured before the connection was lost is saved with the meeting. ' +
  'Start streaming again to continue in a new meeting.';

const byteLength = (data) => data?.byteLength ?? data?.length ?? 0;

export const createTranscriberStream = ({
  resumeWindowMs = RESUME_WINDOW_MS,
  bufferSeconds = RECONNECT_BUFFER_SECONDS,
  onResumeExpired = () => {},
  log = () => {},
  setTimer = (fn, ms) => setTimeout(fn, ms),
  clearTimer = (timer) => clearTimeout(timer),
} = {}) => {
  // The connection the page has open, or null between connections.
  let socket = null;
  // The connection this meeting's START was last sent on.
  let startSentOn = null;
  // The meeting in progress, or null.
  let meeting = null;
  let audio = [];
  let audioBytes = 0;
  let droppedBytes = 0;
  // Text frames waiting for the next START.
  let waiting = [];
  let expiryTimer = null;

  // A socket with no readyState is taken to be open: the page is told of an
  // open socket only through its open event.
  const socketOpen = () => socket !== null && (socket.readyState === undefined || socket.readyState === WS_OPEN);
  const startedHere = () => meeting !== null && socketOpen() && startSentOn === socket;

  const cancelExpiry = () => {
    if (expiryTimer !== null) clearTimer(expiryTimer);
    expiryTimer = null;
  };

  const reset = () => {
    cancelExpiry();
    meeting = null;
    startSentOn = null;
    audio = [];
    audioBytes = 0;
    droppedBytes = 0;
    waiting = [];
  };

  const sendStart = () => {
    socket.send(meeting.startFrame);
    startSentOn = socket;
    meeting.startSent = true;
    waiting.splice(0).forEach((frame) => socket.send(frame));
    if (audio.length > 0) {
      const seconds = (bytes) => (bytes / meeting.bytesPerSecond).toFixed(1);
      const dropped = droppedBytes > 0 ? `; ${seconds(droppedBytes)} s did not fit and was dropped` : '';
      log(`Sent ${seconds(audioBytes)} s of audio held while disconnected${dropped}`);
      audio.forEach((chunk) => socket.send(chunk));
    }
    audio = [];
    audioBytes = 0;
    droppedBytes = 0;
    if (meeting.ending) {
      if (meeting.endFrame) socket.send(meeting.endFrame);
      reset();
    }
  };

  const expire = () => {
    expiryTimer = null;
    if (meeting === null || startedHere()) return;
    const userEnded = meeting.ending;
    log(`No connection for ${Math.round(resumeWindowMs / 1000)} s; the transcriber has ended the meeting`);
    reset();
    // A meeting the user had already stopped needs no notice.
    if (!userEnded) onResumeExpired();
  };

  const toText = (frame) => (typeof frame === 'string' ? frame : JSON.stringify(frame));

  return {
    /** A connection opened. */
    opened(ws) {
      socket = ws;
      startSentOn = null;
      if (meeting !== null) {
        cancelExpiry();
        sendStart();
      }
    },

    /** A connection closed. Closes of connections other than the current one are ignored. */
    closed(ws) {
      if (ws !== socket) return;
      socket = null;
      startSentOn = null;
      // Only a meeting the transcriber has started is held by it.
      if (meeting?.startSent && expiryTimer === null) {
        expiryTimer = setTimer(expire, resumeWindowMs);
      }
    },

    /** Start a meeting: START now if connected, otherwise as soon as a connection opens. */
    begin(startFrame) {
      reset();
      const bytesPerSecond = (startFrame.samplingRate || DEFAULT_SAMPLING_RATE) * CHANNELS * BYTES_PER_SAMPLE;
      meeting = {
        startFrame: toText(startFrame),
        bytesPerSecond,
        maxBufferBytes: Math.round(bytesPerSecond * bufferSeconds),
        startSent: false,
        ending: false,
        endFrame: null,
      };
      if (socketOpen()) sendStart();
    },

    /** Audio of the meeting in progress. Returns whether it was sent now. */
    sendAudio(data) {
      if (meeting === null || meeting.ending) return false;
      if (startedHere()) {
        socket.send(data);
        return true;
      }
      audio.push(data);
      audioBytes += byteLength(data);
      while (audioBytes > meeting.maxBufferBytes && audio.length > 0) {
        const oldest = audio.shift();
        audioBytes -= byteLength(oldest);
        droppedBytes += byteLength(oldest);
      }
      return false;
    },

    /**
     * A control frame (string or object). During a meeting it goes after the
     * meeting's START on the connection; outside one it is sent only if a
     * connection is open.
     */
    sendControl(frame) {
      const text = toText(frame);
      if (meeting === null) {
        if (socketOpen()) socket.send(text);
        return;
      }
      if (startedHere()) socket.send(text);
      else waiting.push(text);
    },

    /**
     * End the meeting in progress, sending `endFrame` (none if null). While
     * disconnected, the frames that are waiting and the END are sent after a
     * START on the next connection, if it opens within the resume window.
     */
    end(endFrame = null) {
      if (meeting === null) return;
      const text = endFrame ? toText(endFrame) : null;
      if (startedHere()) {
        if (text) socket.send(text);
        reset();
        return;
      }
      if (!meeting.startSent) {
        // The transcriber never had this meeting.
        reset();
        return;
      }
      meeting.ending = true;
      meeting.endFrame = text;
    },

    /** Forget the meeting without sending anything more: the transcriber has ended it. */
    abandon() {
      reset();
    },

    /** For tests and diagnostics. */
    state() {
      return {
        active: meeting !== null,
        connected: socketOpen(),
        bufferedBytes: audioBytes,
        droppedBytes,
        waitingFrames: waiting.length,
        resumeTimerRunning: expiryTimer !== null,
      };
    },
  };
};
