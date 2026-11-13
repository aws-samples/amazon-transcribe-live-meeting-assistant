/**
 * The screen recording's start is the time of its first captured frame, which
 * FFmpeg reports on stderr -- not the time FFmpeg was launched.
 *
 * The chunks below are FFmpeg 7.1's stderr from a Zoom meeting recorded on a
 * MicroVM, where FFmpeg was launched at 15:02:05.150 and its x11grab input's
 * first frame was at 15:02:31.283. Key frames stamped against the launch time
 * pointed 26 s past their moment in the recording.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';

import { RecordingStartClock } from './video-start-time.js';

const LAUNCHED = Date.parse('2026-11-11T15:02:05.150Z');
const FIRST_FRAME = 1794409351283; // start: 1794409351.282578, to the millisecond
const at = (iso: string) => Date.parse(`2026-11-11T${iso}Z`);

const BANNER = 'ffmpeg version 7.1.5-0+deb13u1 Copyright (c) 2000-2026 the FFmpeg developers\n';
const PROBE = '[x11grab @ 0xaaab133391c0] Stream #0: not enough frames to estimate rate; consider increasing probesize\n';
const X11_INPUT = "Input #0, x11grab, from ':99':\n  Duration: N/A, start: 1794409351.282578, bitrate: 331776 kb/s\n";
const X11_STREAM = '  Stream #0:0: Video: rawvideo (BGR[0] / 0x524742), bgr0, 1920x1080, 331776 kb/s, 5 fps, 1000k tbr, 1000k tbn\n';
const PULSE_INPUT =
    "Input #1, pulse, from 'combined_audio.monitor':\n  Duration: N/A, start: 1794409351.687950, bitrate: 1536 kb/s\n";
const SEGMENT_OPENED = "[segment @ 0xaaab1334c250] Opening '/tmp/video_chunks/segment_000.ts' for writing\n";

test('the start is the first frame FFmpeg reports, not the launch time', () => {
    const clock = new RecordingStartClock(LAUNCHED);
    assert.equal(clock.feed(BANNER, at('15:02:28.726')), false);
    assert.equal(clock.feed(PROBE, at('15:02:31.517')), false);
    assert.equal(clock.startMs, null, 'unknown until FFmpeg has captured a frame');
    assert.equal(clock.feed(X11_INPUT + X11_STREAM, at('15:02:31.656')), true);
    assert.equal(clock.startMs, FIRST_FRAME);
    assert.equal(clock.source, 'first-frame');
    // 26.1 s after launch: the gap the stored offsets were late by.
    assert.equal((clock.startMs! - LAUNCHED) / 1000, 26.133);
    // Later output, the audio input's own start and the first segment included, changes nothing.
    assert.equal(clock.feed(PULSE_INPUT + SEGMENT_OPENED, at('15:02:32.844')), false);
    assert.equal(clock.startMs, FIRST_FRAME);
});

test('a description split across chunks is read once it is whole', () => {
    const clock = new RecordingStartClock(LAUNCHED);
    const whole = X11_INPUT + X11_STREAM;
    // Split mid-number: "start: 17944" must not be taken as a time.
    const cut = whole.indexOf('09351.28');
    assert.equal(clock.feed(whole.slice(0, cut), at('15:02:31.600')), false);
    assert.equal(clock.startMs, null);
    assert.equal(clock.feed(whole.slice(cut), at('15:02:31.656')), true);
    assert.equal(clock.startMs, FIRST_FRAME);
});

test('the audio input start is never taken for the video start', () => {
    const clock = new RecordingStartClock(LAUNCHED);
    assert.equal(clock.feed(PULSE_INPUT, at('15:02:31.788')), false);
    assert.equal(clock.startMs, null);
});

test('without a first-frame time, the first segment opening is used', () => {
    const clock = new RecordingStartClock(LAUNCHED);
    clock.feed(BANNER + "Input #0, x11grab, from ':99':\n  Duration: N/A, bitrate: 331776 kb/s\n", at('15:02:31.656'));
    assert.equal(clock.startMs, null);
    assert.equal(clock.feed(SEGMENT_OPENED, at('15:02:32.844')), true);
    assert.equal(clock.startMs, at('15:02:32.844'));
    assert.equal(clock.source, 'first-segment');
});

test('a start that is not a wall-clock time is ignored', () => {
    // A build stamping frames with a monotonic clock reports seconds since boot.
    const clock = new RecordingStartClock(LAUNCHED);
    clock.feed("Input #0, x11grab, from ':99':\n  Duration: N/A, start: 8123.456000, bitrate: 331776 kb/s\n", at('15:02:31.656'));
    assert.equal(clock.startMs, null);
    clock.feed(SEGMENT_OPENED, at('15:02:32.844'));
    assert.equal(clock.startMs, at('15:02:32.844'));
    assert.equal(clock.source, 'first-segment');
});
