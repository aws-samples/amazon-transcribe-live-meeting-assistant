/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { createTranscriberStream, RESUME_WINDOW_MS } from './transcriber-stream';

const START = { callEvent: 'START', callId: 'Stand-up - 2026-11-04', samplingRate: 8000, consent: { mode: 'off' } };
const END = { callEvent: 'END', callId: 'Stand-up - 2026-11-04' };

// A socket that records what is sent on it, as text or as a byte count.
const fakeSocket = (name) => {
  const sent = [];
  return {
    name,
    sent,
    readyState: 1,
    send: (data) => sent.push(typeof data === 'string' ? JSON.parse(data) : { audio: data.byteLength, id: data.id }),
  };
};
const chunk = (bytes, id) => Object.assign(new Uint8Array(bytes), { id });
const events = (socket) => socket.sent.map((f) => f.callEvent ?? (f.audio !== undefined ? 'audio' : '?'));

describe('transcriber stream', () => {
  let onResumeExpired;
  let stream;

  beforeEach(() => {
    vi.useFakeTimers();
    onResumeExpired = vi.fn();
    stream = createTranscriberStream({ onResumeExpired });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('sends START once when the meeting begins on an open connection, then audio as it comes', () => {
    const a = fakeSocket('a');
    stream.opened(a);
    stream.begin(START);
    stream.sendAudio(chunk(100));
    stream.sendAudio(chunk(100));
    expect(events(a)).toEqual(['START', 'audio', 'audio']);
    expect(a.sent[0]).toEqual(START);
  });

  it('sends START once, on the connection that opens, when the meeting begins before it is open', () => {
    stream.begin(START);
    stream.sendAudio(chunk(100));
    const a = fakeSocket('a');
    stream.opened(a);
    stream.sendAudio(chunk(100));
    expect(events(a)).toEqual(['START', 'audio', 'audio']);
  });

  it('sends nothing on a connection that opens with no meeting in progress', () => {
    const a = fakeSocket('a');
    stream.opened(a);
    stream.sendAudio(chunk(100));
    expect(a.sent).toEqual([]);
  });

  it('sends the same START again on a reconnect, before the audio held while disconnected', () => {
    const a = fakeSocket('a');
    stream.opened(a);
    stream.begin(START);
    stream.sendAudio(chunk(100, 'before'));
    stream.closed(a);
    stream.sendAudio(chunk(100, 'down-1'));
    stream.sendAudio(chunk(100, 'down-2'));

    const b = fakeSocket('b');
    stream.opened(b);
    stream.sendAudio(chunk(100, 'after'));

    expect(events(a)).toEqual(['START', 'audio']);
    expect(events(b)).toEqual(['START', 'audio', 'audio', 'audio']);
    expect(b.sent[0]).toEqual(a.sent[0]);
    expect(b.sent.slice(1).map((f) => f.id)).toEqual(['down-1', 'down-2', 'after']);
    expect(stream.state().resumeTimerRunning).toBe(false);
  });

  it('holds at most the buffer window of audio while disconnected, dropping the oldest', () => {
    // 8 kHz stereo 16-bit is 32000 bytes a second; 3 seconds is 96000 bytes.
    const a = fakeSocket('a');
    stream.opened(a);
    stream.begin(START);
    stream.closed(a);
    for (let i = 0; i < 150; i += 1) stream.sendAudio(chunk(1000, i));
    expect(stream.state().bufferedBytes).toBe(96000);
    expect(stream.state().droppedBytes).toBe(54000);

    const b = fakeSocket('b');
    stream.opened(b);
    const held = b.sent.slice(1);
    expect(held).toHaveLength(96);
    expect(held[0].id).toBe(54);
    expect(held[95].id).toBe(149);
    expect(stream.state().bufferedBytes).toBe(0);
  });

  it('sends control frames that waited for a reconnect after its START and before the held audio', () => {
    const a = fakeSocket('a');
    stream.opened(a);
    stream.begin(START);
    stream.closed(a);
    stream.sendAudio(chunk(100));
    stream.sendControl({ callEvent: 'CONSENT_OBJECTION', source: 'voice' });
    const b = fakeSocket('b');
    stream.opened(b);
    expect(events(b)).toEqual(['START', 'CONSENT_OBJECTION', 'audio']);
  });

  it('ends a meeting stopped while disconnected after a START on the next connection, then goes idle', () => {
    const a = fakeSocket('a');
    stream.opened(a);
    stream.begin(START);
    stream.closed(a);
    stream.sendAudio(chunk(100));
    stream.end(END);
    stream.sendAudio(chunk(100));

    const b = fakeSocket('b');
    stream.opened(b);
    expect(events(b)).toEqual(['START', 'audio', 'END']);
    expect(stream.state().active).toBe(false);

    const c = fakeSocket('c');
    stream.opened(c);
    expect(c.sent).toEqual([]);
  });

  it('sends END at once when connected, and nothing for the meeting afterwards', () => {
    const a = fakeSocket('a');
    stream.opened(a);
    stream.begin(START);
    stream.end(END);
    stream.sendAudio(chunk(100));
    expect(events(a)).toEqual(['START', 'END']);
    const b = fakeSocket('b');
    stream.closed(a);
    stream.opened(b);
    expect(b.sent).toEqual([]);
  });

  it('ends the stream and reports it when no connection opens within the resume window', () => {
    const a = fakeSocket('a');
    stream.opened(a);
    stream.begin(START);
    stream.closed(a);
    vi.advanceTimersByTime(RESUME_WINDOW_MS - 1);
    expect(onResumeExpired).not.toHaveBeenCalled();
    vi.advanceTimersByTime(1);
    expect(onResumeExpired).toHaveBeenCalledTimes(1);
    expect(stream.state().active).toBe(false);

    // A START now would start a second session of the ended meeting.
    const b = fakeSocket('b');
    stream.opened(b);
    stream.sendAudio(chunk(100));
    expect(b.sent).toEqual([]);
  });

  it('counts the resume window from the first close, not from each failed attempt', () => {
    const a = fakeSocket('a');
    stream.opened(a);
    stream.begin(START);
    stream.closed(a);
    vi.advanceTimersByTime(30000);
    // A failed attempt closes a socket that never opened.
    stream.closed(fakeSocket('failed'));
    vi.advanceTimersByTime(RESUME_WINDOW_MS - 30000);
    expect(onResumeExpired).toHaveBeenCalledTimes(1);
  });

  it('does not report a lost meeting the user had already stopped', () => {
    const a = fakeSocket('a');
    stream.opened(a);
    stream.begin(START);
    stream.closed(a);
    stream.end(END);
    vi.advanceTimersByTime(RESUME_WINDOW_MS);
    expect(onResumeExpired).not.toHaveBeenCalled();
    expect(stream.state().active).toBe(false);
  });

  it('does not report a meeting that reconnected in time', () => {
    const a = fakeSocket('a');
    stream.opened(a);
    stream.begin(START);
    stream.closed(a);
    stream.opened(fakeSocket('b'));
    vi.advanceTimersByTime(RESUME_WINDOW_MS * 2);
    expect(onResumeExpired).not.toHaveBeenCalled();
    expect(stream.state().active).toBe(true);
  });

  it('ignores the close of a connection that is no longer the current one', () => {
    const a = fakeSocket('a');
    const b = fakeSocket('b');
    stream.opened(a);
    stream.begin(START);
    stream.opened(b);
    stream.closed(a);
    stream.sendAudio(chunk(100));
    expect(events(b)).toEqual(['START', 'audio']);
    expect(stream.state().resumeTimerRunning).toBe(false);
  });

  it('holds audio rather than send it on a connection that is closing', () => {
    const a = fakeSocket('a');
    stream.opened(a);
    stream.begin(START);
    a.readyState = 2;
    stream.sendAudio(chunk(100));
    expect(events(a)).toEqual(['START']);
    expect(stream.state().bufferedBytes).toBe(100);
  });

  it('sends nothing more for a meeting the transcriber ended', () => {
    const a = fakeSocket('a');
    stream.opened(a);
    stream.begin(START);
    stream.abandon();
    stream.closed(a);
    const b = fakeSocket('b');
    stream.opened(b);
    stream.sendAudio(chunk(100));
    expect(events(a)).toEqual(['START']);
    expect(b.sent).toEqual([]);
    vi.advanceTimersByTime(RESUME_WINDOW_MS);
    expect(onResumeExpired).not.toHaveBeenCalled();
  });

  it('forgets a meeting stopped before any connection opened, without creating it', () => {
    stream.begin(START);
    stream.end(END);
    const a = fakeSocket('a');
    stream.opened(a);
    expect(a.sent).toEqual([]);
  });
});
