/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

// Reads MP4 box headers and sample flags, which are bit fields.
/* eslint-disable no-bitwise */

/**
 * Screen video from the Stream Audio/Video page and the browser extension: the
 * shared tab, window or screen, recorded with the browser's MediaRecorder as
 * fragmented MP4 and sent to the WebSocket transcriber on a second connection,
 * the way the Desktop Capture App sends its video
 * (docs/websocket-streaming-api.md, "6.1.6 Screen Video"): START_VIDEO with the
 * callId and the video's offset from the audio, the chunks as binary frames,
 * then END_VIDEO.
 *
 * The transcriber samples only the stream's H.264 key frames, and needs one at
 * least every two seconds. MediaRecorder is asked for one every 1.8 seconds
 * (2 seconds gave 2.2-second gaps at 5 frames a second, because the encoder
 * keys on the first frame after the interval), and the spacing the encoder
 * actually produces is read back from the stream (createKeyframeMonitor), since
 * hardware encoders may not honour the option.
 *
 * Framework-free, with the browser objects injectable, so it is tested without
 * a browser. StreamAudio.jsx wires it to the page; the extension keeps an
 * identical copy (lma-browser-extension-stack/src/lib/screen-video.js, held to
 * this one by a test) and uses its connection and key frame monitor for the
 * chunks its in-tab recorder hands over.
 */

/** A browser constructor by name, or undefined outside a browser. */
const browserGlobal = (name) => (typeof window === 'undefined' ? undefined : window[name]);

/** Tried in order: H.264 High 4.0 first, then whatever H.264 profile the browser picks. */
export const VIDEO_MIME_TYPES = ['video/mp4;codecs=avc1.640028', 'video/mp4;codecs=avc1'];
export const RECORDER_OPTIONS = { videoKeyFrameIntervalDuration: 1800, videoBitsPerSecond: 1500000 };
/** MediaRecorder delivers about one fragment per key frame interval whatever this is. */
export const TIMESLICE_MS = 2000;
/** 5 frames a second, the long edge at most 1920 pixels. */
export const VIDEO_CONSTRAINTS = { frameRate: { ideal: 5, max: 5 }, width: { max: 1920 }, height: { max: 1920 } };
/** The transcriber's key frame lane samples at least every two seconds; a little jitter is allowed. */
export const KEYFRAME_GAP_WARNING_SECONDS = 2.5;

/** The fixed sentence the consent notice gains when screen video is on. */
export const SCREEN_VIDEO_NOTICE = 'The shared screen is also recorded as video.';

/** The first supported MP4 type, or null when this browser cannot record one. */
export const pickVideoMimeType = (Recorder = browserGlobal('MediaRecorder')) => {
  if (!Recorder || typeof Recorder.isTypeSupported !== 'function') return null;
  return VIDEO_MIME_TYPES.find((type) => Recorder.isTypeSupported(type)) || null;
};

/** What the user picked to share, as the track's displaySurface names it. */
export const describeSurface = (displaySurface) => {
  if (displaySurface === 'browser') return { label: 'A browser tab', wholeScreen: false };
  if (displaySurface === 'window') return { label: 'A window', wholeScreen: false };
  if (displaySurface === 'monitor') return { label: 'Your whole screen', wholeScreen: true };
  return { label: 'The surface you chose', wholeScreen: false };
};

export const WHOLE_SCREEN_WARNING =
  'You are sharing your whole screen. Everything on it is recorded as video, including notifications and ' +
  'other windows. Share only the meeting tab or window to keep the rest private.';

// ── key frame spacing ──────────────────────────────────────────────────────

const ascii = (bytes, at) => String.fromCharCode(bytes[at], bytes[at + 1], bytes[at + 2], bytes[at + 3]);
const u32 = (bytes, at) => ((bytes[at] << 24) >>> 0) + (bytes[at + 1] << 16) + (bytes[at + 2] << 8) + bytes[at + 3];
const u64 = (bytes, at) => u32(bytes, at) * 2 ** 32 + u32(bytes, at + 4);

/** The child boxes of the payload [start, end). */
const childBoxes = (bytes, start, end) => {
  const boxes = [];
  let at = start;
  while (at + 8 <= end) {
    let size = u32(bytes, at);
    let header = 8;
    if (size === 1) {
      size = u64(bytes, at + 8);
      header = 16;
    } else if (size === 0) {
      size = end - at;
    }
    if (size < header || at + size > end) break;
    boxes.push({ type: ascii(bytes, at + 4), start: at + header, end: at + size });
    at += size;
  }
  return boxes;
};

const child = (bytes, box, type) => box && childBoxes(bytes, box.start, box.end).find((b) => b.type === type);

const NON_SYNC = 0x10000;

/**
 * Reads the H.264 key frame times of a fragmented MP4 as its chunks arrive,
 * and calls `onSlowKeyframes(gapSeconds)` once when two key frames are further
 * apart than `warnAboveSeconds`. Chunks may split boxes anywhere; media data is
 * skipped rather than held.
 */
export const createKeyframeMonitor = ({
  warnAboveSeconds = KEYFRAME_GAP_WARNING_SECONDS,
  onSlowKeyframes = () => {},
} = {}) => {
  let pending = new Uint8Array(0);
  let skip = 0;
  let timescale = 0;
  let defaultFlags = 0;
  let lastKeyframe = null;
  let keyframes = 0;
  let maxGap = 0;
  let warned = false;

  const keyframeAt = (seconds) => {
    keyframes += 1;
    if (lastKeyframe !== null) {
      const gap = seconds - lastKeyframe;
      maxGap = Math.max(maxGap, gap);
      if (gap > warnAboveSeconds && !warned) {
        warned = true;
        onSlowKeyframes(Math.round(gap * 10) / 10);
      }
    }
    lastKeyframe = seconds;
  };

  const readMoov = (bytes, box) => {
    const trak = child(bytes, box, 'trak');
    const mdhd = child(bytes, child(bytes, trak, 'mdia'), 'mdhd');
    if (mdhd) timescale = bytes[mdhd.start] === 1 ? u32(bytes, mdhd.start + 20) : u32(bytes, mdhd.start + 12);
    const trex = child(bytes, child(bytes, box, 'mvex'), 'trex');
    if (trex) defaultFlags = u32(bytes, trex.start + 20);
  };

  const readMoof = (bytes, box) => {
    if (!timescale) return;
    childBoxes(bytes, box.start, box.end)
      .filter((b) => b.type === 'traf')
      .forEach((traf) => {
        const tfhd = child(bytes, traf, 'tfhd');
        const tfdt = child(bytes, traf, 'tfdt');
        if (!tfhd || !tfdt) return;
        const tfhdFlags = u32(bytes, tfhd.start) & 0xffffff;
        let at = tfhd.start + 8;
        if (tfhdFlags & 0x01) at += 8;
        if (tfhdFlags & 0x02) at += 4;
        let duration = 0;
        if (tfhdFlags & 0x08) {
          duration = u32(bytes, at);
          at += 4;
        }
        if (tfhdFlags & 0x10) at += 4;
        let flags = defaultFlags;
        if (tfhdFlags & 0x20) flags = u32(bytes, at);
        let time = bytes[tfdt.start] === 1 ? u64(bytes, tfdt.start + 4) : u32(bytes, tfdt.start + 4);
        childBoxes(bytes, traf.start, traf.end)
          .filter((b) => b.type === 'trun')
          .forEach((trun) => {
            const trunFlags = u32(bytes, trun.start) & 0xffffff;
            const count = u32(bytes, trun.start + 4);
            let p = trun.start + 8;
            if (trunFlags & 0x01) p += 4;
            let firstFlags = null;
            if (trunFlags & 0x04) {
              firstFlags = u32(bytes, p);
              p += 4;
            }
            for (let i = 0; i < count && p <= trun.end; i += 1) {
              let sampleDuration = duration;
              let sampleFlags = i === 0 && firstFlags !== null ? firstFlags : flags;
              if (trunFlags & 0x100) {
                sampleDuration = u32(bytes, p);
                p += 4;
              }
              if (trunFlags & 0x200) p += 4;
              if (trunFlags & 0x400) {
                sampleFlags = u32(bytes, p);
                p += 4;
              }
              if (trunFlags & 0x800) p += 4;
              if (!(sampleFlags & NON_SYNC)) keyframeAt(time / timescale);
              time += sampleDuration;
            }
          });
      });
  };

  const push = (chunk) => {
    let bytes = chunk instanceof Uint8Array ? chunk : new Uint8Array(chunk);
    if (skip) {
      const skipped = Math.min(skip, bytes.length);
      skip -= skipped;
      bytes = bytes.subarray(skipped);
    }
    if (!bytes.length) return;
    const joined = new Uint8Array(pending.length + bytes.length);
    joined.set(pending);
    joined.set(bytes, pending.length);
    pending = joined;
    while (pending.length >= 8) {
      let size = u32(pending, 0);
      let header = 8;
      if (size === 1) {
        if (pending.length < 16) return;
        size = u64(pending, 8);
        header = 16;
      }
      if (size < header) {
        // Not an MP4 box stream: stop reading rather than misreport.
        pending = new Uint8Array(0);
        return;
      }
      const type = ascii(pending, 4);
      if (type === 'mdat') {
        skip = Math.max(0, size - pending.length);
        pending = pending.subarray(Math.min(size, pending.length));
        if (skip) return;
      } else {
        if (pending.length < size) return;
        if (type === 'moov') readMoov(pending, { start: header, end: size });
        if (type === 'moof') readMoof(pending, { start: header, end: size });
        pending = pending.slice(size);
      }
    }
  };

  return {
    push,
    keyframes: () => keyframes,
    maxGapSeconds: () => maxGap,
  };
};

// ── the video connection ───────────────────────────────────────────────────

/** Why the transcriber closes a video connection (docs/websocket-streaming-api.md, "6.1.6 Screen Video"). */
export const CLOSE_NO_ACTIVE_CALL = 'no active call for callId';
export const CLOSE_NOT_ENABLED = 'screen video is not enabled for this client';
export const CLOSE_CONSENT = 'meeting consent does not allow capture';
const POLICY_CLOSE = 1008;

/** What the page tells the user when the video stops on its own. */
export const videoStopMessage = (reason) => {
  if (reason === CLOSE_NOT_ENABLED) {
    return 'Screen video is not enabled on this deployment, so it was not recorded. Audio is still being streamed.';
  }
  if (reason === CLOSE_CONSENT) return "The meeting's consent does not allow capture, so screen video stopped.";
  if (reason === CLOSE_NO_ACTIVE_CALL) {
    return "LMA did not find this meeting's audio stream, so screen video stopped. Audio is still being streamed.";
  }
  if (reason === 'ended') return 'You stopped sharing, so screen video stopped. Audio is still being streamed.';
  if (reason === 'lost' || reason === 'buffer') {
    return (
      'The screen video connection was lost and could not be restored, so screen video stopped. ' +
      'Audio is still being streamed.'
    );
  }
  return 'Screen video was not accepted for this meeting, so it stopped. Audio is still being streamed.';
};

/** 1 s, 2 s, 4 s, then every 5 s, as the audio connection reconnects. */
const reconnectDelay = (attempt) => Math.min(1000 * 2 ** attempt, 5000);

/**
 * The video connection of one meeting, kept going across drops.
 *
 * The connection opens with the first chunk, by when the meeting's audio START
 * has normally been handled. A START_VIDEO refused because the audio session
 * is not there yet is tried again from the start of the stream, so every chunk
 * is kept until the transcriber says it has a session for the stream
 * (VIDEO_STARTED), or, from one that does not say, until the connection has
 * stayed open for `confirmMs`. Any other drop of a stream the transcriber took
 * reconnects with `videoResume` and sends what was recorded meanwhile (at most
 * `maxBufferBytes`), for up to `resumeWindowMs`, which is inside the
 * transcriber's two-minute hold; a stream that never reached it starts again
 * from its first chunk. Any other refusal ends the video for the meeting; the
 * audio is not affected.
 */
export const createVideoUplink = ({
  openSocket,
  callId,
  offsetMs,
  onStopped = () => {},
  log = () => {},
  confirmMs = 5000,
  maxStartAttempts = 6,
  startRetryMs = 1500,
  resumeWindowMs = 110 * 1000,
  maxBufferBytes = 32 * 1024 * 1024,
  setTimer = (fn, ms) => setTimeout(fn, ms),
  clearTimer = (timer) => clearTimeout(timer),
  now = () => Date.now(),
}) => {
  let socket = null;
  let open = false;
  let confirmed = false;
  let confirmTimer = null;
  let retryTimer = null;
  let startAttempts = 0;
  let reconnectAttempts = 0;
  let droppedAt = null;
  let stopping = false;
  let finished = false;
  // Kept until confirmed: everything since the stream began.
  let retained = [];
  let retainedBytes = 0;
  // Recorded while no connection was open.
  let queued = [];
  let queuedBytes = 0;
  let connecting = false;
  // Whether the transcriber holds a video session for this stream: it said so
  // (VIDEO_STARTED), or, from a transcriber that does not say, the connection
  // stayed open for `confirmMs`. A later connection then resumes that session.
  let admitted = false;

  const admit = () => {
    admitted = true;
    confirmed = true;
    retained = [];
    retainedBytes = 0;
  };

  const finish = (reason) => {
    if (finished) return;
    finished = true;
    if (confirmTimer !== null) clearTimer(confirmTimer);
    if (retryTimer !== null) clearTimer(retryTimer);
    retained = [];
    queued = [];
    onStopped(reason);
  };

  const sendJson = (frame) => socket.send(JSON.stringify(frame));

  const connect = async () => {
    if (connecting || finished) return;
    connecting = true;
    const resume = admitted;
    let ws;
    try {
      ws = await openSocket();
    } catch (error) {
      connecting = false;
      log(`video connection could not open: ${error?.message || error}`);
      // eslint-disable-next-line no-use-before-define
      scheduleReconnect();
      return;
    }
    connecting = false;
    if (finished) {
      ws.close?.(1000);
      return;
    }
    socket = ws;
    ws.binaryType = 'arraybuffer';
    ws.onopen = () => {
      open = true;
      droppedAt = null;
      reconnectAttempts = 0;
      sendJson({
        callEvent: 'START_VIDEO',
        callId,
        videoTimeOffsetMs: offsetMs,
        ...(resume ? { videoResume: true } : {}),
      });
      // A resumed stream carries on from what the last connection sent; a new
      // one starts again from its first chunk, which holds the init segment.
      const backlog = resume ? queued : retained;
      backlog.forEach((chunk) => ws.send(chunk));
      queued = [];
      queuedBytes = 0;
      if (!confirmed) {
        confirmTimer = setTimer(() => {
          confirmTimer = null;
          if (socket === ws && open) admit();
        }, confirmMs);
      }
      if (stopping) {
        sendJson({ callEvent: 'END_VIDEO', callId });
        ws.close(1000);
      }
    };
    ws.onclose = (event) => {
      if (socket !== ws) return;
      open = false;
      socket = null;
      if (confirmTimer !== null) {
        clearTimer(confirmTimer);
        confirmTimer = null;
      }
      if (stopping) {
        finish('stopped');
        return;
      }
      const reason = event?.reason || '';
      if (event?.code === POLICY_CLOSE) {
        if (reason === CLOSE_NO_ACTIVE_CALL && !confirmed && startAttempts + 1 < maxStartAttempts) {
          startAttempts += 1;
          // No session was made for it: the retry starts the stream again.
          admitted = false;
          queued = [];
          queuedBytes = 0;
          log(`video refused before the audio session was found; trying again (${startAttempts})`);
          retryTimer = setTimer(() => {
            retryTimer = null;
            connect();
          }, startRetryMs);
          return;
        }
        finish(reason || 'refused');
        return;
      }
      // Dropped: reconnect, resuming the session it was admitted to.
      // eslint-disable-next-line no-use-before-define
      scheduleReconnect();
    };
    ws.onmessage = (event) => {
      if (typeof event?.data !== 'string') return;
      try {
        if (JSON.parse(event.data)?.event === 'VIDEO_STARTED' && socket === ws) {
          if (confirmTimer !== null) {
            clearTimer(confirmTimer);
            confirmTimer = null;
          }
          admit();
        }
      } catch {
        // not a frame of ours
      }
    };
    ws.onerror = () => log('video connection error');
  };

  const scheduleReconnect = () => {
    if (finished || stopping) {
      if (stopping) finish('stopped');
      return;
    }
    if (droppedAt === null) droppedAt = now();
    if (now() - droppedAt > resumeWindowMs) {
      finish('lost');
      return;
    }
    const delay = reconnectDelay(reconnectAttempts);
    reconnectAttempts += 1;
    retryTimer = setTimer(() => {
      retryTimer = null;
      connect();
    }, delay);
  };

  const send = (chunk) => {
    if (finished || stopping) return;
    const bytes = chunk?.byteLength ?? 0;
    if (!confirmed) {
      retained.push(chunk);
      retainedBytes += bytes;
      if (retainedBytes > maxBufferBytes) {
        log('video buffer full before the stream was confirmed; giving up on video');
        finish('buffer');
        return;
      }
    }
    if (open && socket) {
      socket.send(chunk);
      return;
    }
    if (admitted) {
      queued.push(chunk);
      queuedBytes += bytes;
      if (queuedBytes > maxBufferBytes) {
        log('video buffer full while disconnected; giving up on video');
        finish('buffer');
        return;
      }
    }
    if (!socket && !connecting && retryTimer === null) connect();
  };

  const stop = () => {
    if (finished || stopping) return;
    stopping = true;
    if (open && socket) {
      sendJson({ callEvent: 'END_VIDEO', callId });
      socket.close(1000);
      return;
    }
    if (!socket && !connecting) finish('stopped');
  };

  return { send, stop, isFinished: () => finished };
};

// ── the recorder ───────────────────────────────────────────────────────────

/**
 * Records the shared surface's video track and sends it to the transcriber.
 * `onEvent` hears `{ type: 'slow-keyframes', gapSeconds }` and, once, `{ type:
 * 'stopped', reason }` (reason `stopped` when the page stopped it). Returns `{
 * stop }`; stopping twice, or after the video stopped on its own, does nothing.
 */
export const startScreenVideo = ({
  stream,
  callId,
  offsetMs,
  openSocket,
  onEvent = () => {},
  log = () => {},
  Recorder = browserGlobal('MediaRecorder'),
  MediaStreamClass = browserGlobal('MediaStream'),
  uplinkOptions = {},
}) => {
  const mimeType = pickVideoMimeType(Recorder);
  if (!mimeType) throw new Error('This browser cannot record screen video as MP4.');
  const [track] = stream.getVideoTracks();
  if (!track) throw new Error('The shared surface has no video.');
  try {
    // Text and slides over motion: sharper key frames for the slides.
    track.contentHint = 'detail';
  } catch {
    // Not settable in this browser.
  }
  let stoppedReason = null;
  let uplinkStopped = false;
  const uplink = createVideoUplink({
    openSocket,
    callId,
    offsetMs: Math.max(0, Math.round(offsetMs)),
    log,
    ...uplinkOptions,
    onStopped: (reason) => {
      uplinkStopped = true;
      if (stoppedReason === null) stoppedReason = reason;
      // eslint-disable-next-line no-use-before-define
      if (recorder.state !== 'inactive') recorder.stop();
      onEvent({ type: 'stopped', reason: stoppedReason });
    },
  });
  const monitor = createKeyframeMonitor({
    onSlowKeyframes: (gapSeconds) => onEvent({ type: 'slow-keyframes', gapSeconds }),
  });
  const recorder = new Recorder(new MediaStreamClass([track]), { mimeType, ...RECORDER_OPTIONS });
  let chain = Promise.resolve();
  recorder.ondataavailable = (event) => {
    if (!event.data || !event.data.size) return;
    chain = chain.then(async () => {
      const buffer = await event.data.arrayBuffer();
      monitor.push(new Uint8Array(buffer));
      uplink.send(buffer);
    });
  };
  recorder.onstop = () => {
    chain = chain.then(() => {
      if (!uplinkStopped) uplink.stop();
    });
  };
  recorder.onerror = (event) => log(`MediaRecorder error: ${event?.error?.name || event}`);
  const onTrackEnded = () => {
    if (stoppedReason === null) stoppedReason = 'ended';
    if (recorder.state !== 'inactive') recorder.stop();
  };
  track.addEventListener?.('ended', onTrackEnded);
  recorder.start(TIMESLICE_MS);
  log(`screen video recording as ${mimeType}`);

  return {
    mimeType,
    stop: () => {
      if (stoppedReason === null) stoppedReason = 'stopped';
      track.removeEventListener?.('ended', onTrackEnded);
      if (recorder.state !== 'inactive') recorder.stop();
    },
    keyframeMonitor: monitor,
  };
};
