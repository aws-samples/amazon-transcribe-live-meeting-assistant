/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The arithmetic behind the meeting page's player, kept free of React so it
 * can be tested directly: which recording it plays, how a time on the
 * transcript's clock maps to a position in that recording and back, which
 * transcript segment is being heard, where the chapters and the screen shares
 * sit on the seek bar, and which key presses control it.
 *
 * Two clocks. Transcript segments, chapters and key frames carry times on the
 * transcript's clock (seconds from when transcription of the meeting started).
 * A recording has its own (seconds from the start of the file). Their
 * difference, the `offset`, is the transcript time at a recording position
 * minus that position:
 *
 *   transcript seconds = recording seconds + offset
 *
 * For the audio recording, and for every source but the Virtual Participant's
 * video, the offset is 0: the file starts with transcription. A Virtual
 * Participant starts its screen recording before it is admitted to the
 * meeting, so its video runs ahead of the transcript by the time it waited,
 * and by more after each reconnection of its transcription, which the video
 * records through and the transcript skips. (A pause does not move them apart:
 * the transcript's clock runs on through it, and the audio recording keeps it
 * as silence.) Each key frame records both clocks at the
 * moment it was taken (Timestamp and VideoOffset), so the frames are the
 * anchors: a time is converted with the offset of the nearest anchor before
 * it. See docs/web-ui-guide.md (Meeting Detail Page) for each source.
 *
 * A `clock` is { anchors: [{ transcript, recording }] in time order }; with no
 * anchors the offset is 0.
 */

/** How far the arrow keys move the recording. */
export const SEEK_STEP_SECONDS = 5;

/**
 * A segment still counts as being heard for this long after its end, so the
 * highlight does not flicker off in the short pauses between two lines.
 */
export const SEGMENT_HOLD_SECONDS = 1.5;

const finite = (value) => {
  if (value === null || value === undefined || value === '') return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
};

/** The clock of a recording with no anchors: the transcript's own. */
export const IDENTITY_CLOCK = Object.freeze({ anchors: [] });

/**
 * The anchors between the transcript's clock and the video recording's: one
 * per key frame that carries both its transcript time and its position in the
 * recording, in transcript-time order.
 */
export const clockAnchorsFromFrames = (frames) =>
  (frames || [])
    .filter((f) => finite(f?.VideoOffset) !== null && finite(f?.Timestamp) !== null)
    .map((f) => ({ transcript: Number(f.Timestamp), recording: Number(f.VideoOffset) }))
    .sort((a, b) => a.transcript - b.transcript);

/** Whether two anchor lists are the same, so an unchanged list need not re-render the page. */
export const sameAnchors = (a, b) =>
  a === b ||
  (a?.length === b?.length &&
    (a || []).every((x, i) => x.transcript === b[i].transcript && x.recording === b[i].recording));

// The offset of the last anchor at or before `value` on the given side, or of
// the first anchor when `value` is before them all; 0 with no anchors.
const offsetNear = (clock, value, side) => {
  const anchors = clock?.anchors || [];
  if (!anchors.length) return 0;
  let chosen = anchors[0];
  anchors.forEach((anchor) => {
    if (anchor[side] <= value) chosen = anchor;
  });
  return chosen.transcript - chosen.recording;
};

/** The offset in effect at a time on the transcript's clock. */
export const offsetAtTranscript = (clock, transcriptSeconds) =>
  offsetNear(clock, finite(transcriptSeconds) ?? 0, 'transcript');

/** The position in the recording for a time on the transcript's clock. Never before the start. */
export const toRecordingSeconds = (clock, transcriptSeconds) => {
  const t = finite(transcriptSeconds);
  if (t === null) return 0;
  return Math.max(0, t - offsetNear(clock, t, 'transcript'));
};

/**
 * The time on the transcript's clock for a position in the recording. Never
 * before the transcript's start: a Virtual Participant's video begins in the
 * lobby, before anything was transcribed.
 */
export const toTranscriptSeconds = (clock, recordingSeconds) => {
  const r = finite(recordingSeconds);
  if (r === null) return 0;
  return Math.max(0, r + offsetNear(clock, r, 'recording'));
};

/**
 * What the meeting page's player plays. The video recording when there is one
 * -- its file carries the meeting's audio as well -- and otherwise the audio
 * recording; `prefer: 'audio'` asks for the audio recording when both exist.
 * Nothing while the meeting is live: neither file exists until it has ended.
 * Returns { kind, url, alternative } (alternative: the other kind, when the
 * meeting has both), or null when there is nothing to play.
 */
export const chooseMeetingMedia = ({ recordingUrl, videoRecordingUrl, isLive = false, prefer = 'video' } = {}) => {
  if (isLive) return null;
  const both = Boolean(recordingUrl && videoRecordingUrl);
  if (videoRecordingUrl && !(both && prefer === 'audio')) {
    return { kind: 'video', url: videoRecordingUrl, alternative: both ? 'audio' : null };
  }
  if (recordingUrl) return { kind: 'audio', url: recordingUrl, alternative: both ? 'video' : null };
  return null;
};

/** Why the player starts on the audio recording, said beside it. */
export const AUDIO_DEFAULT_REASON =
  "Playing the audio recording, which lines up with the transcript. This Virtual Participant meeting's video has " +
  'no key frames to line it up with, and its video starts before transcription. Choose Video to watch it.';

/**
 * Which recording the player starts on when the viewer has not chosen: the
 * video, except for a Virtual Participant meeting whose video has no key
 * frame to line it up with -- that video starts before transcription by an
 * unknown time, while the audio recording starts with it. Decided only once
 * the key frames have loaded, so a meeting is not switched for frames still
 * on their way. Returns { prefer, reason } (reason: why the audio, or null).
 */
export const defaultMediaPreference = ({
  hasBoth = false,
  isVirtualParticipant = false,
  framesLoaded = false,
  hasAnchors = false,
} = {}) =>
  hasBoth && isVirtualParticipant && framesLoaded && !hasAnchors
    ? { prefer: 'audio', reason: AUDIO_DEFAULT_REASON }
    : { prefer: 'video', reason: null };

/** The clock of the recording a player plays: anchored on the key frames for video, the transcript's for audio. */
export const clockFor = (kind, anchors) => (kind === 'video' && anchors?.length ? { anchors } : IDENTITY_CLOCK);

/**
 * Where the player should go for a key frame: the frame's own position in the
 * video recording when the player is playing that recording and the frame
 * knows it, and otherwise its transcript time converted on the clock.
 */
export const recordingSecondsForFrame = (frame, { kind, clock = IDENTITY_CLOCK } = {}) => {
  const videoOffset = finite(frame?.VideoOffset);
  if (kind === 'video' && videoOffset !== null) return Math.max(0, videoOffset);
  return toRecordingSeconds(clock, frame?.Timestamp);
};

/**
 * The id of the transcript segment being heard at `seconds` (transcript
 * clock): of the segments under way then -- started, and not ended more than
 * SEGMENT_HOLD_SECONDS ago -- the one that started last. Null in a silence
 * longer than that, or before the first segment.
 *
 * `segments` are { id, startTime, endTime }, in any order.
 */
export const currentSegmentId = (segments, seconds, hold = SEGMENT_HOLD_SECONDS) => {
  const t = finite(seconds);
  if (t === null || !segments?.length) return null;
  let best = null;
  let bestStart = -Infinity;
  segments.forEach((segment) => {
    const start = finite(segment?.startTime);
    if (start === null || start > t) return;
    const end = finite(segment.endTime) ?? start;
    if (t > Math.max(end, start) + hold) return;
    if (start >= bestStart) {
      best = segment.id;
      bestStart = start;
    }
  });
  return best;
};

/**
 * The chapters as marks on a seek bar of a recording `durationSeconds` long:
 * each at its share of the bar (0-100), at the recording position of its
 * transcript time. A chapter past the end of the recording is left off; with
 * no known duration there are no marks.
 */
export const chapterMarkers = (chapters, durationSeconds, clock = IDENTITY_CLOCK) => {
  const duration = finite(durationSeconds);
  if (!duration || duration <= 0 || !chapters?.length) return [];
  return chapters
    .map((chapter) => {
      const recordingSeconds = toRecordingSeconds(clock, chapter.seconds);
      return {
        key: `${chapter.seconds}`,
        title: chapter.title,
        label: chapter.label,
        transcriptSeconds: chapter.seconds,
        recordingSeconds,
        percent: (recordingSeconds / duration) * 100,
      };
    })
    .filter((marker) => marker.recordingSeconds <= duration)
    .map((marker) => ({ ...marker, percent: Math.min(100, Math.max(0, marker.percent)) }));
};

/** The chapter playing at a recording position: the last one that started at or before it. */
export const chapterAt = (markers, recordingSeconds) => {
  const r = finite(recordingSeconds);
  if (r === null || !markers?.length) return null;
  return markers.reduce((found, marker) => (marker.recordingSeconds <= r ? marker : found), null);
};

/** "m:ss" under an hour, "h:mm:ss" from an hour. */
export const formatClock = (seconds) => {
  const total = Math.max(0, Math.floor(finite(seconds) ?? 0));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = String(total % 60).padStart(2, '0');
  return h ? `${h}:${String(m).padStart(2, '0')}:${s}` : `${m}:${s}`;
};

/**
 * The meeting's screen shares as marks on the same seek bar: one per key frame
 * of the shared screen that the Screen share tab shows (`frames`: FrameId,
 * Timestamp, VideoOffset, Title), at the frame's place in the recording being
 * played (recordingSecondsForFrame). A frame past the end of the recording is
 * left off; with no known duration there are no marks. These are a different
 * thing from the chapters, which come from the summary: a chapter is a change
 * of topic and a screen share a change of what was on screen, so a meeting
 * usually has a different number of each.
 */
export const screenShareMarkers = (frames, durationSeconds, { kind = 'video', clock = IDENTITY_CLOCK } = {}) => {
  const duration = finite(durationSeconds);
  if (!duration || duration <= 0 || !frames?.length) return [];
  return frames
    .filter((frame) => frame?.FrameId && finite(frame.Timestamp) !== null)
    .map((frame) => {
      const recordingSeconds = recordingSecondsForFrame(frame, { kind, clock });
      const title = typeof frame.Title === 'string' ? frame.Title.trim() : '';
      return {
        key: frame.FrameId,
        frameId: frame.FrameId,
        title,
        label: formatClock(Number(frame.Timestamp)),
        transcriptSeconds: Number(frame.Timestamp),
        recordingSeconds,
        percent: Math.min(100, Math.max(0, (recordingSeconds / duration) * 100)),
      };
    })
    .filter((marker) => marker.recordingSeconds <= duration);
};

const TYPING_TAGS = ['INPUT', 'TEXTAREA', 'SELECT'];
// Elements where Space already means something: pressing a button or a link.
const SPACE_TAGS = ['BUTTON', 'A', 'SUMMARY'];
const SPACE_ROLES = ['button', 'link', 'checkbox', 'switch', 'tab', 'menuitem', 'option', 'radio'];

/**
 * The player action for a key press, or null to leave it alone: Space plays
 * or pauses, Left and Right move 5 seconds. Never while focus is in a field
 * or an editable element, or with a modifier held; Space is also left to a
 * focused button, link or other control it already activates, and the arrows
 * to a focused slider, which moves itself.
 */
export const playerShortcut = (event) => {
  if (!event || event.defaultPrevented) return null;
  if (event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return null;
  const target = event.target || {};
  const tag = String(target.tagName || '').toUpperCase();
  const role = typeof target.getAttribute === 'function' ? target.getAttribute('role') : null;
  if (TYPING_TAGS.includes(tag) || target.isContentEditable) return null;
  if (role === 'textbox' || role === 'combobox' || role === 'searchbox') return null;
  if (event.key === ' ' || event.code === 'Space') {
    if (SPACE_TAGS.includes(tag) || SPACE_ROLES.includes(role)) return null;
    return 'toggle';
  }
  if (role === 'slider') return null;
  if (event.key === 'ArrowLeft') return 'back';
  if (event.key === 'ArrowRight') return 'forward';
  return null;
};

/*
 * Capture sessions. A meeting is captured in one session unless it is
 * continued (a Virtual Participant rejoin, or a streaming client's "Continue
 * an existing meeting"), which adds another. The transcript holds every
 * session on one timeline -- each later session starts at its offset, after
 * the earlier ones -- while each session is recorded in a file of its own that
 * starts at 0. A session's recording is therefore on a clock offset by where
 * its session starts: one anchor, { transcript: offset, recording: 0 }, or the
 * session's own key frames for a video that has them.
 *
 * A `session` is { number, offset, end, recordingUrl, videoRecordingUrl }: its
 * session number, where it starts and where the next one starts (Infinity for
 * the last) on the transcript's clock, and its recordings.
 */

const sessionNumberOf = (value) => {
  const n = finite(value);
  return n !== null && Number.isInteger(n) && n >= 1 ? n : null;
};

// Where each session starts: the first at 0, each later one as Call.Sessions
// lists it. A session that started before Sessions was kept is known only when
// it is the latest (Call.SessionOffsetSeconds).
const sessionOffsets = (meeting) => {
  const offsets = new Map([[1, 0]]);
  (Array.isArray(meeting?.sessions) ? meeting.sessions : []).forEach((entry) => {
    const number = sessionNumberOf(entry?.SessionNumber);
    const offset = finite(entry?.OffsetSeconds);
    if (number && number > 1 && offset !== null && offset >= 0) offsets.set(number, offset);
  });
  const latest = sessionNumberOf(meeting?.sessionStartedNumber);
  const latestOffset = finite(meeting?.sessionOffsetSeconds);
  if (
    latest &&
    latest > 1 &&
    !offsets.has(latest) &&
    sessionNumberOf(meeting?.sessionNumber) === latest &&
    latestOffset !== null &&
    latestOffset >= 0
  ) {
    offsets.set(latest, latestOffset);
  }
  return offsets;
};

// Each session's link of one kind: the links paired with the sessions stored
// beside them, the last link of a session winning (a redelivered write repeats
// one). Links stored without their sessions -- a meeting recorded before they
// were kept -- are paired in order when there is exactly one per session.
const linksBySession = (urls, tags, numbers) => {
  const links = new Map();
  if (!Array.isArray(urls) || !urls.length) return links;
  if (Array.isArray(tags) && tags.length === urls.length) {
    urls.forEach((url, i) => {
      const number = sessionNumberOf(tags[i]);
      if (url && number) links.set(number, url);
    });
    return links;
  }
  const distinct = [...new Set(urls.filter(Boolean))];
  if (distinct.length === numbers.length) distinct.forEach((url, i) => links.set(numbers[i], url));
  return links;
};

/**
 * The meeting's sessions that have a recording, in order, for the player. A
 * meeting that was never continued -- or whose sessions cannot be told apart
 * -- is one session at 0 with the meeting's RecordingUrl and VideoRecordingUrl,
 * which is how the player has always played it.
 *
 * `meeting` is the mapped meeting: recordingUrl, videoRecordingUrl,
 * recordingUrls, recordingUrlSessions, videoRecordingUrls,
 * videoRecordingUrlSessions, sessions, sessionNumber, sessionOffsetSeconds and
 * sessionStartedNumber.
 */
export const playbackSessions = (meeting) => {
  const offsets = sessionOffsets(meeting);
  const latest = sessionNumberOf(meeting?.sessionStartedNumber) || 1;
  const single = [
    {
      number: latest,
      offset: offsets.get(latest) ?? 0,
      end: Infinity,
      recordingUrl: meeting?.recordingUrl || null,
      videoRecordingUrl: meeting?.videoRecordingUrl || null,
    },
  ];
  if (offsets.size < 2) return single;
  const numbers = [...offsets.keys()].sort((a, b) => a - b);
  const audio = linksBySession(meeting?.recordingUrls, meeting?.recordingUrlSessions, numbers);
  const video = linksBySession(meeting?.videoRecordingUrls, meeting?.videoRecordingUrlSessions, numbers);
  const recorded = numbers
    .filter((number) => audio.has(number) || video.has(number))
    .map((number) => ({
      number,
      offset: offsets.get(number),
      recordingUrl: audio.get(number) || null,
      videoRecordingUrl: video.get(number) || null,
    }));
  if (!recorded.length) return single;
  return recorded.map((session, i) => ({ ...session, end: recorded[i + 1]?.offset ?? Infinity }));
};

/** The session playing at a time on the transcript's clock: the last that started by then, else the first. */
export const sessionAtTranscript = (sessions, transcriptSeconds) => {
  if (!sessions?.length) return null;
  const t = finite(transcriptSeconds) ?? 0;
  return sessions.reduce((found, session) => (session.offset <= t ? session : found), sessions[0]);
};

/** The session after `session`, or null. */
export const nextSession = (sessions, session) => {
  const i = (sessions || []).findIndex((s) => s.number === session?.number);
  return i >= 0 && i + 1 < sessions.length ? sessions[i + 1] : null;
};

/** Whether a time on the transcript's clock falls within the session. */
export const inSession = (session, transcriptSeconds) => {
  const t = finite(transcriptSeconds);
  if (!session || t === null) return false;
  return t >= session.offset && t < session.end;
};

// A session that is the meeting's whole timeline: nothing to filter or move.
const wholeTimeline = (session) => !session || (session.offset === 0 && session.end === Infinity);

/**
 * The items of `items` within the session, by the transcript time `secondsOf`
 * gives each: the chapters and screen shares the seek bar of that session's
 * recording marks. All of them for a meeting of one session.
 */
export const withinSession = (items, session, secondsOf) =>
  wholeTimeline(session) ? items || [] : (items || []).filter((item) => inSession(session, secondsOf(item)));

/**
 * The clock of the recording a player plays for a session: the video's key
 * frames within that session when it has some, and otherwise the session's
 * start, where its file starts. For a meeting of one session it is clockFor's.
 */
export const sessionClock = (kind, session, anchors) => {
  if (wholeTimeline(session)) return clockFor(kind, anchors);
  const own = (anchors || []).filter((anchor) => inSession(session, anchor.transcript));
  if (kind === 'video' && own.length) return { anchors: own };
  return { anchors: [{ transcript: session.offset, recording: 0 }] };
};

/** The key frame anchors within a session, for deciding which of its recordings to play first. */
export const sessionAnchors = (session, anchors) =>
  wholeTimeline(session) ? anchors || [] : (anchors || []).filter((anchor) => inSession(session, anchor.transcript));

/** How the session selector names a session: in order, with where it starts on the transcript. */
export const sessionLabel = (sessions, session) => {
  const i = (sessions || []).findIndex((s) => s.number === session?.number);
  return `Session ${i + 1} (from ${formatClock(session?.offset)})`;
};
