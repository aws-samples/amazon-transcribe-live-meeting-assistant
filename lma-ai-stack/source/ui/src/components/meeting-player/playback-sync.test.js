/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import { describe, it, expect } from 'vitest';

import {
  IDENTITY_CLOCK,
  chapterAt,
  chapterMarkers,
  chooseMeetingMedia,
  clockAnchorsFromFrames,
  clockFor,
  currentSegmentId,
  defaultMediaPreference,
  formatClock,
  inSession,
  nextSession,
  offsetAtTranscript,
  playbackSessions,
  playerShortcut,
  recordingSecondsForFrame,
  sameAnchors,
  screenShareMarkers,
  sessionAnchors,
  sessionAtTranscript,
  sessionClock,
  sessionLabel,
  toRecordingSeconds,
  toTranscriptSeconds,
  withinSession,
} from './playback-sync';

// A Virtual Participant's video: 20 s in the lobby before transcription began,
// then a 30 s pause at 5:00 that the video recorded through.
const VP_CLOCK = {
  anchors: [
    { transcript: 60, recording: 80 },
    { transcript: 400, recording: 450 },
  ],
};

describe('mapping between the transcript and a recording', () => {
  it('is the identity for a recording that starts with transcription', () => {
    expect(toRecordingSeconds(IDENTITY_CLOCK, 125.5)).toBe(125.5);
    expect(toTranscriptSeconds(IDENTITY_CLOCK, 125.5)).toBe(125.5);
  });

  it('uses the offset of the nearest key frame before the time', () => {
    expect(toRecordingSeconds(VP_CLOCK, 100)).toBe(120);
    expect(toRecordingSeconds(VP_CLOCK, 400)).toBe(450);
    expect(toRecordingSeconds(VP_CLOCK, 500)).toBe(550);
    expect(offsetAtTranscript(VP_CLOCK, 500)).toBe(-50);
  });

  it('uses the first frame for times before it', () => {
    expect(toRecordingSeconds(VP_CLOCK, 10)).toBe(30);
    expect(toTranscriptSeconds(VP_CLOCK, 30)).toBe(10);
  });

  it('maps back from the recording on the same anchors', () => {
    expect(toTranscriptSeconds(VP_CLOCK, 120)).toBe(100);
    expect(toTranscriptSeconds(VP_CLOCK, 550)).toBe(500);
  });

  it('round-trips a time between two anchors', () => {
    [0, 59, 61, 250, 399, 401, 900].forEach((t) => {
      expect(toTranscriptSeconds(VP_CLOCK, toRecordingSeconds(VP_CLOCK, t))).toBe(t);
    });
  });

  it('never goes before the start of either clock', () => {
    expect(toRecordingSeconds({ anchors: [{ transcript: 30, recording: 0 }] }, 10)).toBe(0);
    // The lobby, recorded before anything was transcribed.
    expect(toTranscriptSeconds(VP_CLOCK, 5)).toBe(0);
  });

  it('treats a missing time as the start', () => {
    expect(toRecordingSeconds(VP_CLOCK, undefined)).toBe(0);
    expect(toTranscriptSeconds(IDENTITY_CLOCK, 'x')).toBe(0);
  });
});

describe('clockAnchorsFromFrames', () => {
  it('keeps the frames that carry both clocks, in transcript order', () => {
    expect(
      clockAnchorsFromFrames([
        { Timestamp: 400, VideoOffset: 450 },
        { Timestamp: 200, VideoOffset: null },
        { Timestamp: 60, VideoOffset: 80 },
        { Timestamp: 90 },
        { Timestamp: 'x', VideoOffset: 5 },
      ]),
    ).toEqual(VP_CLOCK.anchors);
  });

  it('keeps an offset of zero', () => {
    expect(clockAnchorsFromFrames([{ Timestamp: 0, VideoOffset: 0 }])).toEqual([{ transcript: 0, recording: 0 }]);
  });

  it('compares lists by value', () => {
    expect(sameAnchors(VP_CLOCK.anchors, clockAnchorsFromFrames([{ Timestamp: 60, VideoOffset: 80 }]))).toBe(false);
    expect(sameAnchors([{ transcript: 1, recording: 2 }], [{ transcript: 1, recording: 2 }])).toBe(true);
  });
});

describe('chooseMeetingMedia', () => {
  const urls = { recordingUrl: 'https://a/audio.wav', videoRecordingUrl: 'https://a/video.mp4' };

  it('plays the video when there is one, offering the audio as the alternative', () => {
    expect(chooseMeetingMedia(urls)).toEqual({ kind: 'video', url: urls.videoRecordingUrl, alternative: 'audio' });
  });

  it('plays the audio when asked to, or when it is all there is', () => {
    expect(chooseMeetingMedia({ ...urls, prefer: 'audio' })).toEqual({
      kind: 'audio',
      url: urls.recordingUrl,
      alternative: 'video',
    });
    expect(chooseMeetingMedia({ recordingUrl: urls.recordingUrl })).toEqual({
      kind: 'audio',
      url: urls.recordingUrl,
      alternative: null,
    });
    expect(chooseMeetingMedia({ videoRecordingUrl: urls.videoRecordingUrl, prefer: 'audio' }).kind).toBe('video');
  });

  it('plays nothing while the meeting is live, or with no recording', () => {
    expect(chooseMeetingMedia({ ...urls, isLive: true })).toBeNull();
    expect(chooseMeetingMedia({})).toBeNull();
  });

  it('anchors only the video on the key frames', () => {
    expect(clockFor('video', VP_CLOCK.anchors)).toEqual(VP_CLOCK);
    expect(clockFor('audio', VP_CLOCK.anchors)).toBe(IDENTITY_CLOCK);
    expect(clockFor('video', [])).toBe(IDENTITY_CLOCK);
  });
});

describe('recordingSecondsForFrame', () => {
  it("uses a frame's own place in the video when it has one", () => {
    expect(recordingSecondsForFrame({ Timestamp: 100, VideoOffset: 131 }, { kind: 'video', clock: VP_CLOCK })).toBe(
      131,
    );
  });

  it('converts its transcript time otherwise, and always for the audio', () => {
    expect(recordingSecondsForFrame({ Timestamp: 100, VideoOffset: null }, { kind: 'video', clock: VP_CLOCK })).toBe(
      120,
    );
    expect(recordingSecondsForFrame({ Timestamp: 100, VideoOffset: 131 }, { kind: 'audio' })).toBe(100);
  });
});

describe('currentSegmentId', () => {
  const segments = [
    { id: 'a', startTime: 0, endTime: 4 },
    { id: 'b', startTime: 5, endTime: 9 },
    { id: 'c', startTime: 7, endTime: 12 },
    { id: 'd', startTime: 30, endTime: 35 },
  ];

  it('is the segment under way', () => {
    expect(currentSegmentId(segments, 2)).toBe('a');
    expect(currentSegmentId(segments, 5)).toBe('b');
  });

  it('is the later of two that overlap', () => {
    expect(currentSegmentId(segments, 8)).toBe('c');
  });

  it('holds a segment through a short pause after it', () => {
    expect(currentSegmentId(segments, 4.5)).toBe('a');
    expect(currentSegmentId(segments, 13)).toBe('c');
  });

  it('is none in a longer silence, before the first segment, or with no time', () => {
    expect(currentSegmentId(segments, 20)).toBeNull();
    expect(currentSegmentId([{ id: 'x', startTime: 10, endTime: 12 }], 3)).toBeNull();
    expect(currentSegmentId(segments, null)).toBeNull();
    expect(currentSegmentId([], 3)).toBeNull();
  });

  it('does not depend on the order of the segments', () => {
    expect(currentSegmentId([...segments].reverse(), 8)).toBe('c');
  });
});

describe('chapterMarkers', () => {
  const chapters = [
    { seconds: 0, label: '00:00', title: 'Welcome' },
    { seconds: 300, label: '05:00', title: 'Budget' },
    { seconds: 1200, label: '20:00', title: 'Past the end' },
  ];

  it('places each chapter at its share of the recording', () => {
    const markers = chapterMarkers(chapters, 600);
    expect(markers.map((m) => [m.title, m.percent, m.recordingSeconds])).toEqual([
      ['Welcome', 0, 0],
      ['Budget', 50, 300],
    ]);
  });

  it('places them on the recording clock, not the transcript clock', () => {
    const markers = chapterMarkers(chapters, 600, { anchors: [{ transcript: 0, recording: 60 }] });
    expect(markers.map((m) => [m.title, m.percent, m.recordingSeconds, m.transcriptSeconds])).toEqual([
      ['Welcome', 10, 60, 0],
      ['Budget', 60, 360, 300],
    ]);
  });

  it('has no marks before the recording length is known', () => {
    expect(chapterMarkers(chapters, 0)).toEqual([]);
    expect(chapterMarkers(chapters, NaN)).toEqual([]);
    expect(chapterMarkers([], 600)).toEqual([]);
  });

  it('names the chapter playing at a position', () => {
    const markers = chapterMarkers(chapters, 600);
    expect(chapterAt(markers, 10).title).toBe('Welcome');
    expect(chapterAt(markers, 300).title).toBe('Budget');
    expect(chapterAt([], 10)).toBeNull();
  });
});

describe('screenShareMarkers', () => {
  const frames = [
    { FrameId: '0000030000-a', Timestamp: 30, VideoOffset: 50, Title: ' Q3 budget ' },
    { FrameId: '0000300000-b', Timestamp: 300, VideoOffset: 330, Title: null },
    { FrameId: '0001200000-c', Timestamp: 1200, VideoOffset: 1230, Title: 'Past the end' },
  ];

  it("places each frame at its own place in the video recording, named by the frame's title", () => {
    const markers = screenShareMarkers(frames, 600, { kind: 'video' });
    expect(markers.map((m) => [m.frameId, m.title, m.label, m.recordingSeconds, Math.round(m.percent)])).toEqual([
      ['0000030000-a', 'Q3 budget', '0:30', 50, 8],
      ['0000300000-b', '', '5:00', 330, 55],
    ]);
  });

  it('places each frame by its transcript time on the audio recording', () => {
    const markers = screenShareMarkers(frames, 600, { kind: 'audio' });
    expect(markers.map((m) => m.recordingSeconds)).toEqual([30, 300]);
  });

  it('has no marks before the recording length is known, or without frames', () => {
    expect(screenShareMarkers(frames, 0)).toEqual([]);
    expect(screenShareMarkers([], 600)).toEqual([]);
    expect(screenShareMarkers(undefined, 600)).toEqual([]);
  });
});

describe('formatClock', () => {
  it('reads m:ss under an hour and h:mm:ss from one', () => {
    expect(formatClock(0)).toBe('0:00');
    expect(formatClock(65.9)).toBe('1:05');
    expect(formatClock(3723)).toBe('1:02:03');
    expect(formatClock(undefined)).toBe('0:00');
  });
});

describe('playerShortcut', () => {
  const key = (k, target = document.body, extra = {}) => ({ key: k, code: k === ' ' ? 'Space' : k, target, ...extra });
  const el = (tag, attrs = {}) => {
    const node = document.createElement(tag);
    Object.entries(attrs).forEach(([name, value]) => node.setAttribute(name, value));
    return node;
  };

  it('plays or pauses on Space and moves on the arrows', () => {
    expect(playerShortcut(key(' '))).toBe('toggle');
    expect(playerShortcut(key('ArrowLeft'))).toBe('back');
    expect(playerShortcut(key('ArrowRight'))).toBe('forward');
    expect(playerShortcut(key('ArrowUp'))).toBeNull();
    expect(playerShortcut(key(' ', el('div')))).toBe('toggle');
  });

  it('leaves typing alone', () => {
    expect(playerShortcut(key(' ', el('input')))).toBeNull();
    expect(playerShortcut(key('ArrowLeft', el('textarea')))).toBeNull();
    expect(playerShortcut(key(' ', el('div', { role: 'combobox' })))).toBeNull();
    const editable = el('div');
    Object.defineProperty(editable, 'isContentEditable', { value: true });
    expect(playerShortcut(key(' ', editable))).toBeNull();
  });

  it('leaves Space to a focused control it already presses, and the arrows to a slider', () => {
    expect(playerShortcut(key(' ', el('button')))).toBeNull();
    expect(playerShortcut(key(' ', el('a')))).toBeNull();
    expect(playerShortcut(key(' ', el('div', { role: 'tab' })))).toBeNull();
    expect(playerShortcut(key('ArrowRight', el('button')))).toBe('forward');
    expect(playerShortcut(key('ArrowRight', el('div', { role: 'slider' })))).toBeNull();
  });

  it('leaves a key with a modifier, or one already handled, alone', () => {
    expect(playerShortcut(key(' ', document.body, { ctrlKey: true }))).toBeNull();
    expect(playerShortcut(key('ArrowLeft', document.body, { altKey: true }))).toBeNull();
    expect(playerShortcut(key(' ', document.body, { defaultPrevented: true }))).toBeNull();
  });
});

describe('defaultMediaPreference', () => {
  const vp = { hasBoth: true, isVirtualParticipant: true, framesLoaded: true };

  it('starts a Virtual Participant meeting whose video has no key frames on the audio, saying why', () => {
    const choice = defaultMediaPreference({ ...vp, hasAnchors: false });
    expect(choice.prefer).toBe('audio');
    expect(choice.reason).toMatch(/no key frames/);
  });

  it('starts on the video otherwise', () => {
    expect(defaultMediaPreference({ ...vp, hasAnchors: true })).toEqual({ prefer: 'video', reason: null });
    expect(defaultMediaPreference({ ...vp, isVirtualParticipant: false }).prefer).toBe('video');
    expect(defaultMediaPreference({ ...vp, hasBoth: false }).prefer).toBe('video');
    // Not before the key frames have been read.
    expect(defaultMediaPreference({ ...vp, framesLoaded: false }).prefer).toBe('video');
  });
});

// A meeting rejoined twice: session 2 starts at 317 s, session 3 at 905 s on
// the transcript's clock, and each session is recorded in a file of its own.
const CONTINUED = {
  recordingUrl: 'https://b/a3.wav',
  videoRecordingUrl: 'https://b/v3.mp4',
  recordingUrls: ['https://b/a1.wav', 'https://b/a2.wav', 'https://b/a3.wav'],
  recordingUrlSessions: [1, 2, 3],
  videoRecordingUrls: ['https://b/v1.mp4', 'https://b/v3.mp4'],
  videoRecordingUrlSessions: [1, 3],
  sessions: [
    { SessionNumber: 2, OffsetSeconds: 317, StartedAt: '2026-11-19T10:10:00Z' },
    { SessionNumber: 3, OffsetSeconds: 905, StartedAt: '2026-11-19T10:30:00Z' },
  ],
  sessionNumber: 3,
  sessionOffsetSeconds: 905,
  sessionStartedNumber: 3,
};

describe("a meeting's capture sessions", () => {
  it("are one session at 0 with the meeting's recordings when it was never continued", () => {
    const meeting = { recordingUrl: 'https://b/a.wav', videoRecordingUrl: 'https://b/v.mp4' };
    expect(playbackSessions(meeting)).toEqual([
      { number: 1, offset: 0, end: Infinity, recordingUrl: 'https://b/a.wav', videoRecordingUrl: 'https://b/v.mp4' },
    ]);
    // The lists a single session also has change nothing.
    expect(
      playbackSessions({ ...meeting, recordingUrls: ['https://b/a.wav'], recordingUrlSessions: [1] }),
    ).toHaveLength(1);
  });

  it("give each session its own recordings, its start and the next one's", () => {
    expect(playbackSessions(CONTINUED)).toEqual([
      { number: 1, offset: 0, end: 317, recordingUrl: 'https://b/a1.wav', videoRecordingUrl: 'https://b/v1.mp4' },
      { number: 2, offset: 317, end: 905, recordingUrl: 'https://b/a2.wav', videoRecordingUrl: null },
      {
        number: 3,
        offset: 905,
        end: Infinity,
        recordingUrl: 'https://b/a3.wav',
        videoRecordingUrl: 'https://b/v3.mp4',
      },
    ]);
  });

  it('leave out a session with no recording, and close the gap', () => {
    const meeting = {
      ...CONTINUED,
      recordingUrls: ['https://b/a1.wav', 'https://b/a3.wav'],
      recordingUrlSessions: [1, 3],
      videoRecordingUrls: [],
      videoRecordingUrlSessions: [],
    };
    expect(playbackSessions(meeting).map(({ number, offset, end }) => [number, offset, end])).toEqual([
      [1, 0, 905],
      [3, 905, Infinity],
    ]);
  });

  it('take a link written twice once, and a link with no session not at all', () => {
    const meeting = {
      ...CONTINUED,
      recordingUrls: ['https://b/a1.wav', 'https://b/a1.wav', 'https://b/x.wav', 'https://b/a2.wav'],
      recordingUrlSessions: [1, 1, null, 2],
      videoRecordingUrls: [],
      videoRecordingUrlSessions: [],
    };
    expect(playbackSessions(meeting).map((s) => [s.number, s.recordingUrl])).toEqual([
      [1, 'https://b/a1.wav'],
      [2, 'https://b/a2.wav'],
    ]);
  });

  it('pair links stored without their sessions in order, when there is one per session', () => {
    const meeting = { ...CONTINUED, recordingUrlSessions: undefined, videoRecordingUrls: undefined };
    expect(playbackSessions(meeting).map((s) => s.recordingUrl)).toEqual([
      'https://b/a1.wav',
      'https://b/a2.wav',
      'https://b/a3.wav',
    ]);
  });

  it("play the latest recording at the latest session's start when the sessions cannot be told apart", () => {
    const meeting = {
      ...CONTINUED,
      recordingUrls: ['https://b/a1.wav', 'https://b/a3.wav'],
      recordingUrlSessions: undefined,
      videoRecordingUrls: undefined,
    };
    expect(playbackSessions(meeting)).toEqual([
      {
        number: 3,
        offset: 905,
        end: Infinity,
        recordingUrl: 'https://b/a3.wav',
        videoRecordingUrl: 'https://b/v3.mp4',
      },
    ]);
  });

  it('know the latest session from the record when Sessions does not list it', () => {
    const meeting = {
      recordingUrl: 'https://b/a2.wav',
      recordingUrls: ['https://b/a1.wav', 'https://b/a2.wav'],
      recordingUrlSessions: [1, 2],
      sessionNumber: 2,
      sessionOffsetSeconds: 317,
      sessionStartedNumber: 2,
    };
    expect(playbackSessions(meeting).map((s) => [s.number, s.offset])).toEqual([
      [1, 0],
      [2, 317],
    ]);
    // Not when a later session was allowed since: that offset is not this session's.
    expect(playbackSessions({ ...meeting, sessionNumber: 3, sessionOffsetSeconds: 700 })).toHaveLength(1);
  });

  it('find the session a transcript time falls in, and the next one', () => {
    const sessions = playbackSessions(CONTINUED);
    expect(sessionAtTranscript(sessions, 20).number).toBe(1);
    expect(sessionAtTranscript(sessions, 317).number).toBe(2);
    expect(sessionAtTranscript(sessions, 904.9).number).toBe(2);
    expect(sessionAtTranscript(sessions, 2000).number).toBe(3);
    expect(sessionAtTranscript(sessions, null).number).toBe(1);
    expect(sessionAtTranscript([], 5)).toBeNull();
    expect(nextSession(sessions, sessions[0]).number).toBe(2);
    expect(nextSession(sessions, sessions[2])).toBeNull();
    expect(inSession(sessions[1], 316.9)).toBe(false);
    expect(inSession(sessions[1], 317)).toBe(true);
    expect(inSession(sessions[1], 905)).toBe(false);
  });

  it("map a later session's transcript time to its own file at t - offset, and back", () => {
    const [, second] = playbackSessions(CONTINUED);
    const clock = sessionClock('audio', second, []);
    expect(toRecordingSeconds(clock, 330)).toBe(13);
    expect(toTranscriptSeconds(clock, 13)).toBe(330);
    // The follow-along highlight reads the transcript time back.
    expect(currentSegmentId([{ id: 's3', startTime: 330, endTime: 340 }], toTranscriptSeconds(clock, 15))).toBe('s3');
  });

  it("line a session's video up on its own key frames, not on another session's", () => {
    const [first, second, third] = playbackSessions(CONTINUED);
    // Session 3's Virtual Participant waited 40 s in the lobby; session 1's 20 s.
    const anchors = [
      { transcript: 60, recording: 80 },
      { transcript: 1000, recording: 135 },
    ];
    expect(sessionAnchors(third, anchors)).toEqual([{ transcript: 1000, recording: 135 }]);
    const clock = sessionClock('video', third, anchors);
    expect(toRecordingSeconds(clock, 1000)).toBe(135);
    expect(toTranscriptSeconds(clock, 40)).toBe(905);
    expect(toRecordingSeconds(sessionClock('video', first, anchors), 60)).toBe(80);
    // A session's video without key frames starts where its session does.
    expect(toRecordingSeconds(sessionClock('video', second, anchors), 400)).toBe(83);
  });

  it('keep the clock of a meeting of one session exactly as it was', () => {
    const [only] = playbackSessions({ recordingUrl: 'https://b/a.wav' });
    expect(sessionClock('audio', only, VP_CLOCK.anchors)).toBe(IDENTITY_CLOCK);
    expect(sessionClock('video', only, VP_CLOCK.anchors)).toEqual(clockFor('video', VP_CLOCK.anchors));
    const items = [{ s: -1 }, { s: 5 }, { s: 99999 }];
    expect(withinSession(items, only, (item) => item.s)).toBe(items);
  });

  it("mark on a session's seek bar only what falls within it", () => {
    const [, second] = playbackSessions(CONTINUED);
    const chapters = [{ seconds: 0 }, { seconds: 320 }, { seconds: 910 }];
    expect(withinSession(chapters, second, (chapter) => chapter.seconds)).toEqual([{ seconds: 320 }]);
    const clock = sessionClock('audio', second, []);
    expect(chapterMarkers([{ seconds: 320, title: 'Budget', label: '05:20' }], 600, clock)[0].recordingSeconds).toBe(3);
  });

  it('are named in order, with where each starts', () => {
    const sessions = playbackSessions({
      ...CONTINUED,
      recordingUrls: ['https://b/a1.wav', 'https://b/a3.wav'],
      recordingUrlSessions: [1, 3],
      videoRecordingUrls: [],
      videoRecordingUrlSessions: [],
    });
    expect(sessions.map((s) => sessionLabel(sessions, s))).toEqual(['Session 1 (from 0:00)', 'Session 2 (from 15:05)']);
  });
});
