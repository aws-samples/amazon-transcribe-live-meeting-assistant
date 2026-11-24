/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
/* eslint-disable no-bitwise */
import { describe, expect, it, vi } from 'vitest';

import {
  CLOSE_CONSENT,
  CLOSE_NO_ACTIVE_CALL,
  CLOSE_NOT_ENABLED,
  RECORDER_OPTIONS,
  TIMESLICE_MS,
  createKeyframeMonitor,
  createVideoUplink,
  describeSurface,
  pickVideoMimeType,
  startScreenVideo,
  videoStopMessage,
} from './screen-video';

// ── a synthetic fragmented MP4 ─────────────────────────────────────────────

const be32 = (n) => [(n >>> 24) & 255, (n >>> 16) & 255, (n >>> 8) & 255, n & 255];
const box = (type, ...payload) => {
  const body = payload.flat();
  return [...be32(8 + body.length), ...[...type].map((c) => c.charCodeAt(0)), ...body];
};
const full = (version, flags) => [version, (flags >>> 16) & 255, (flags >>> 8) & 255, flags & 255];
const TIMESCALE = 90000;
const NON_SYNC = 0x10000;

const initSegment = () =>
  Uint8Array.from([
    ...box(
      'ftyp',
      [...'isom'].map((c) => c.charCodeAt(0)),
      be32(0),
    ),
    ...box(
      'moov',
      box('trak', box('mdia', box('mdhd', full(0, 0), be32(0), be32(0), be32(TIMESCALE), be32(0), be32(0)))),
      box('mvex', box('trex', full(0, 0), be32(1), be32(1), be32(0), be32(0), be32(NON_SYNC))),
    ),
  ]);

/** One fragment: `frames` samples of 0.2 s from `startSeconds`, the first a key frame. */
const fragment = (startSeconds, frames = 10, mdatBytes = 64) =>
  Uint8Array.from([
    ...box(
      'moof',
      box('mfhd', full(0, 0), be32(1)),
      box(
        'traf',
        box('tfhd', full(0, 0x08), be32(1), be32(TIMESCALE / 5)),
        box('tfdt', full(1, 0), be32(0), be32(startSeconds * TIMESCALE)),
        box('trun', full(0, 0x001 | 0x004), be32(frames), be32(0), be32(0)),
      ),
    ),
    ...box('mdat', new Array(mdatBytes).fill(7)),
  ]);

const concat = (...parts) => {
  const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
  let at = 0;
  parts.forEach((p) => {
    out.set(p, at);
    at += p.length;
  });
  return out;
};

describe('screen video settings', () => {
  it('picks H.264 High first, then any H.264, and nothing without MP4 support', () => {
    expect(pickVideoMimeType({ isTypeSupported: () => true })).toBe('video/mp4;codecs=avc1.640028');
    expect(pickVideoMimeType({ isTypeSupported: (t) => t === 'video/mp4;codecs=avc1' })).toBe('video/mp4;codecs=avc1');
    expect(pickVideoMimeType({ isTypeSupported: () => false })).toBeNull();
    expect(pickVideoMimeType(undefined)).toBeNull();
  });

  it('asks the encoder for a key frame every 1.8 s and a chunk every 2 s', () => {
    expect(RECORDER_OPTIONS).toEqual({ videoKeyFrameIntervalDuration: 1800, videoBitsPerSecond: 1500000 });
    expect(TIMESLICE_MS).toBe(2000);
  });

  it('names the shared surface and flags a whole screen', () => {
    expect(describeSurface('browser')).toEqual({ label: 'A browser tab', wholeScreen: false });
    expect(describeSurface('window').wholeScreen).toBe(false);
    expect(describeSurface('monitor')).toEqual({ label: 'Your whole screen', wholeScreen: true });
    expect(describeSurface(undefined).wholeScreen).toBe(false);
  });

  it('explains each way the video can stop', () => {
    expect(videoStopMessage(CLOSE_NOT_ENABLED)).toMatch(/not enabled/);
    expect(videoStopMessage(CLOSE_CONSENT)).toMatch(/consent/);
    expect(videoStopMessage('lost')).toMatch(/Audio is still being streamed/);
    expect(videoStopMessage('ended')).toMatch(/stopped sharing/);
  });
});

describe('createKeyframeMonitor', () => {
  it('reads key frames two seconds apart without warning', () => {
    const onSlowKeyframes = vi.fn();
    const monitor = createKeyframeMonitor({ onSlowKeyframes });
    monitor.push(concat(initSegment(), fragment(0)));
    monitor.push(fragment(2));
    monitor.push(fragment(4));
    expect(monitor.keyframes()).toBe(3);
    expect(monitor.maxGapSeconds()).toBeCloseTo(2);
    expect(onSlowKeyframes).not.toHaveBeenCalled();
  });

  it('warns once when the encoder spaces key frames further apart than the lane allows', () => {
    const onSlowKeyframes = vi.fn();
    const monitor = createKeyframeMonitor({ onSlowKeyframes });
    monitor.push(concat(initSegment(), fragment(0, 15)));
    monitor.push(fragment(3, 15));
    monitor.push(fragment(6, 15));
    expect(onSlowKeyframes).toHaveBeenCalledTimes(1);
    expect(onSlowKeyframes).toHaveBeenCalledWith(3);
  });

  it('reads boxes split across chunks, and skips media data it never holds', () => {
    const monitor = createKeyframeMonitor();
    const all = concat(initSegment(), fragment(0, 10, 5000), fragment(2, 10, 5000));
    for (let at = 0; at < all.length; at += 333) monitor.push(all.subarray(at, at + 333));
    expect(monitor.keyframes()).toBe(2);
  });
});

// ── the connection ─────────────────────────────────────────────────────────

const fakeSockets = () => {
  const made = [];
  const openSocket = vi.fn(async () => {
    const ws = {
      sent: [],
      closed: null,
      send(data) {
        this.sent.push(data);
      },
      close(code) {
        this.closed = code;
      },
    };
    made.push(ws);
    return ws;
  });
  const json = (ws) => ws.sent.filter((m) => typeof m === 'string').map((m) => JSON.parse(m));
  const binary = (ws) => ws.sent.filter((m) => typeof m !== 'string');
  return { made, openSocket, json, binary };
};

const timers = () => {
  let clock = 0;
  const pending = [];
  return {
    setTimer: (fn, ms) => {
      const t = { fn, at: clock + ms };
      pending.push(t);
      return t;
    },
    clearTimer: (t) => {
      const i = pending.indexOf(t);
      if (i >= 0) pending.splice(i, 1);
    },
    now: () => clock,
    advance: async (ms) => {
      clock += ms;
      for (;;) {
        pending.sort((a, b) => a.at - b.at);
        if (!pending.length || pending[0].at > clock) break;
        pending.shift().fn();
        // eslint-disable-next-line no-await-in-loop
        await Promise.resolve();
      }
    },
  };
};

const flush = () =>
  new Promise((r) => {
    setTimeout(r, 0);
  });
const chunk = (n) => new Uint8Array([n]).buffer;

describe('createVideoUplink', () => {
  it('opens with the first chunk, announces the stream, and ends it on stop', async () => {
    const s = fakeSockets();
    const onStopped = vi.fn();
    const t = timers();
    const uplink = createVideoUplink({ openSocket: s.openSocket, callId: 'c1', offsetMs: 1200, onStopped, ...t });
    expect(s.openSocket).not.toHaveBeenCalled();
    uplink.send(chunk(0));
    await flush();
    const ws = s.made[0];
    ws.onopen();
    expect(s.json(ws)).toEqual([{ callEvent: 'START_VIDEO', callId: 'c1', videoTimeOffsetMs: 1200 }]);
    expect(s.binary(ws)).toHaveLength(1);
    uplink.send(chunk(1));
    expect(s.binary(ws)).toHaveLength(2);
    uplink.stop();
    expect(s.json(ws).at(-1)).toEqual({ callEvent: 'END_VIDEO', callId: 'c1' });
    expect(ws.closed).toBe(1000);
    ws.onclose({ code: 1000 });
    expect(onStopped).toHaveBeenCalledWith('stopped');
  });

  it('starts again from the first chunk when the audio session was not found yet', async () => {
    const s = fakeSockets();
    const t = timers();
    const uplink = createVideoUplink({ openSocket: s.openSocket, callId: 'c1', offsetMs: 0, ...t });
    uplink.send(chunk(0));
    await flush();
    s.made[0].onopen();
    uplink.send(chunk(1));
    s.made[0].onclose({ code: 1008, reason: CLOSE_NO_ACTIVE_CALL });
    await t.advance(1500);
    await flush();
    const retry = s.made[1];
    retry.onopen();
    expect(s.json(retry)).toEqual([{ callEvent: 'START_VIDEO', callId: 'c1', videoTimeOffsetMs: 0 }]);
    expect(s.binary(retry).map((b) => new Uint8Array(b)[0])).toEqual([0, 1]);
  });

  it('resumes, rather than restarts, a stream that drops before it is confirmed', async () => {
    const s = fakeSockets();
    const t = timers();
    const uplink = createVideoUplink({ openSocket: s.openSocket, callId: 'c1', offsetMs: 0, ...t });
    uplink.send(chunk(0));
    await flush();
    s.made[0].onopen();
    uplink.send(chunk(1));
    // Dropped after one second: the transcriber already holds the session.
    await t.advance(1000);
    s.made[0].onclose({ code: 1006 });
    uplink.send(chunk(2));
    await t.advance(1000);
    await flush();
    const again = s.made[1];
    again.onopen();
    expect(s.json(again)).toEqual([
      { callEvent: 'START_VIDEO', callId: 'c1', videoTimeOffsetMs: 0, videoResume: true },
    ]);
    expect(s.binary(again).map((b) => new Uint8Array(b)[0])).toEqual([2]);
  });

  it('starts again from the first chunk when the first connection never opened', async () => {
    const s = fakeSockets();
    const t = timers();
    const uplink = createVideoUplink({ openSocket: s.openSocket, callId: 'c1', offsetMs: 0, ...t });
    uplink.send(chunk(0));
    await flush();
    s.made[0].onclose({ code: 1006 });
    uplink.send(chunk(1));
    await t.advance(1000);
    await flush();
    const again = s.made[1];
    again.onopen();
    expect(s.json(again)).toEqual([{ callEvent: 'START_VIDEO', callId: 'c1', videoTimeOffsetMs: 0 }]);
    expect(s.binary(again).map((b) => new Uint8Array(b)[0])).toEqual([0, 1]);
  });

  it('resumes a confirmed stream with what was recorded while disconnected', async () => {
    const s = fakeSockets();
    const t = timers();
    const uplink = createVideoUplink({ openSocket: s.openSocket, callId: 'c1', offsetMs: 0, ...t });
    uplink.send(chunk(0));
    await flush();
    s.made[0].onopen();
    await t.advance(5000);
    s.made[0].onclose({ code: 1006 });
    uplink.send(chunk(1));
    uplink.send(chunk(2));
    await t.advance(1000);
    await flush();
    const again = s.made[1];
    again.onopen();
    expect(s.json(again)).toEqual([
      { callEvent: 'START_VIDEO', callId: 'c1', videoTimeOffsetMs: 0, videoResume: true },
    ]);
    expect(s.binary(again).map((b) => new Uint8Array(b)[0])).toEqual([1, 2]);
  });

  it('stops the video, and does not reconnect, when the transcriber refuses it', async () => {
    const s = fakeSockets();
    const onStopped = vi.fn();
    const t = timers();
    const uplink = createVideoUplink({ openSocket: s.openSocket, callId: 'c1', offsetMs: 0, onStopped, ...t });
    uplink.send(chunk(0));
    await flush();
    s.made[0].onopen();
    s.made[0].onclose({ code: 1008, reason: CLOSE_NOT_ENABLED });
    await t.advance(10000);
    expect(onStopped).toHaveBeenCalledWith(CLOSE_NOT_ENABLED);
    expect(s.made).toHaveLength(1);
    uplink.send(chunk(1));
    expect(s.made).toHaveLength(1);
  });

  it('gives up when the connection stays down, or the buffer fills', async () => {
    const s = fakeSockets();
    const onStopped = vi.fn();
    const t = timers();
    const uplink = createVideoUplink({
      openSocket: s.openSocket,
      callId: 'c1',
      offsetMs: 0,
      onStopped,
      maxBufferBytes: 2,
      ...t,
    });
    uplink.send(chunk(0));
    await flush();
    s.made[0].onopen();
    await t.advance(5000);
    s.made[0].onclose({ code: 1006 });
    uplink.send(chunk(1));
    uplink.send(chunk(2));
    uplink.send(chunk(3));
    expect(onStopped).toHaveBeenCalledWith('buffer');

    const lost = vi.fn();
    const s2 = fakeSockets();
    const t2 = timers();
    const u2 = createVideoUplink({ openSocket: s2.openSocket, callId: 'c2', offsetMs: 0, onStopped: lost, ...t2 });
    u2.send(chunk(0));
    await flush();
    s2.made[0].onopen();
    await t2.advance(5000);
    for (let i = 0; i < 40 && !lost.mock.calls.length; i += 1) {
      s2.made.at(-1).onclose?.({ code: 1006 });
      // eslint-disable-next-line no-await-in-loop
      await t2.advance(5000);
      // eslint-disable-next-line no-await-in-loop
      await flush();
    }
    expect(lost).toHaveBeenCalledWith('lost');
  });
});

// ── the recorder ───────────────────────────────────────────────────────────

describe('startScreenVideo', () => {
  const setup = () => {
    const recorders = [];
    function FakeRecorder(stream, options) {
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
      recorders.push(this);
    }
    FakeRecorder.isTypeSupported = () => true;
    function FakeMediaStream(tracks) {
      this.tracks = tracks;
    }
    const track = { contentHint: '', addEventListener: vi.fn(), removeEventListener: vi.fn() };
    const audioTrack = { kind: 'audio' };
    const stream = { getVideoTracks: () => [track], getAudioTracks: () => [audioTrack] };
    return { recorders, FakeRecorder, FakeMediaStream, track, stream };
  };

  it('records only the video track, as MP4 with the key frame interval, in 2-second chunks', () => {
    const f = setup();
    const s = fakeSockets();
    const video = startScreenVideo({
      stream: f.stream,
      callId: 'c1',
      offsetMs: 900.4,
      openSocket: s.openSocket,
      Recorder: f.FakeRecorder,
      MediaStreamClass: f.FakeMediaStream,
    });
    const [rec] = f.recorders;
    expect(rec.stream.tracks).toEqual([f.track]);
    expect(rec.options).toEqual({ mimeType: 'video/mp4;codecs=avc1.640028', ...RECORDER_OPTIONS });
    expect(rec.slice).toBe(2000);
    expect(f.track.contentHint).toBe('detail');
    expect(video.mimeType).toBe('video/mp4;codecs=avc1.640028');
  });

  it('sends each chunk, and reports the stop once', async () => {
    const f = setup();
    const s = fakeSockets();
    const onEvent = vi.fn();
    const video = startScreenVideo({
      stream: f.stream,
      callId: 'c1',
      offsetMs: 0,
      openSocket: s.openSocket,
      onEvent,
      Recorder: f.FakeRecorder,
      MediaStreamClass: f.FakeMediaStream,
    });
    const [rec] = f.recorders;
    const bytes = concat(initSegment(), fragment(0));
    rec.ondataavailable({ data: { size: bytes.length, arrayBuffer: async () => bytes.buffer } });
    await flush();
    await flush();
    const ws = s.made[0];
    ws.onopen();
    expect(s.binary(ws)).toHaveLength(1);
    expect(video.keyframeMonitor.keyframes()).toBe(1);
    video.stop();
    await flush();
    expect(s.json(ws).at(-1)).toEqual({ callEvent: 'END_VIDEO', callId: 'c1' });
    ws.onclose({ code: 1000 });
    video.stop();
    expect(onEvent.mock.calls.filter(([e]) => e.type === 'stopped')).toEqual([
      [{ type: 'stopped', reason: 'stopped' }],
    ]);
  });

  it('stops recording when the transcriber refuses the video', async () => {
    const f = setup();
    const s = fakeSockets();
    const onEvent = vi.fn();
    startScreenVideo({
      stream: f.stream,
      callId: 'c1',
      offsetMs: 0,
      openSocket: s.openSocket,
      onEvent,
      Recorder: f.FakeRecorder,
      MediaStreamClass: f.FakeMediaStream,
    });
    const [rec] = f.recorders;
    rec.ondataavailable({ data: { size: 1, arrayBuffer: async () => chunk(0) } });
    await flush();
    await flush();
    s.made[0].onopen();
    s.made[0].onclose({ code: 1008, reason: CLOSE_CONSENT });
    expect(rec.state).toBe('inactive');
    expect(onEvent).toHaveBeenCalledWith({ type: 'stopped', reason: CLOSE_CONSENT });
  });

  it('refuses to start without MP4 support', () => {
    const f = setup();
    f.FakeRecorder.isTypeSupported = () => false;
    expect(() =>
      startScreenVideo({ stream: f.stream, callId: 'c', offsetMs: 0, openSocket: vi.fn(), Recorder: f.FakeRecorder }),
    ).toThrow(/cannot record/);
  });
});
