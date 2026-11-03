/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * What the meeting page's header says about a meeting: its title, when it
 * started, how long it ran and where it was held. Kept free of React so it can
 * be tested directly.
 */

/**
 * The time stamps the meeting sources append to the name they were given, as
 * the last part of the Meeting ID. Each is anchored to the end, so a name that
 * merely contains a date keeps it.
 *
 * - Virtual Participant (kinesis-stream.ts): `<name>-2026-11-03-14-05-09-123`, UTC.
 * - Stream Audio, its embeddable form and the browser extension
 *   (getTimestampStr, formatTimestamp): `<name> - 2026-11-03-14:05:09.123`, local time.
 * - Desktop capture apps: `<name> - 2026-11-03 14:05`, local time; with no name,
 *   `LMA native prototype - 2026-11-03T14:05:09Z`. The load simulator writes the
 *   same shape without the `Z`.
 * - Upload Audio (upload_meeting_initiator): `<slug>-20261103T140509-1a2b3c4d`,
 *   UTC, with eight hex digits that keep two uploads of one name apart.
 *
 * A Meeting ID supplied by an MCP client or typed by hand matches none of them
 * and is shown as it is.
 */
const MEETING_ID_SUFFIXES = [
  /-\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}-\d{3}$/,
  /\s+-\s+\d{4}-\d{2}-\d{2}[-T ]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,3})?)?Z?$/,
  /-\d{8}T\d{6}-[0-9a-f]{8}$/,
];

/** The meeting's name: its Meeting ID without the time stamp its source appended. */
export const meetingTitle = (callId) => {
  const id = typeof callId === 'string' ? callId : '';
  const suffix = MEETING_ID_SUFFIXES.find((pattern) => pattern.test(id));
  if (!suffix) return id;
  const title = id.replace(suffix, '').trim();
  // A Meeting ID that is nothing but a time stamp keeps it, rather than showing no title.
  return title || id;
};

/**
 * When the meeting started, in the viewer's own time zone (or `timeZone`, for
 * tests), with the zone named: "Nov 3, 2026, 2:05 PM PST". Empty for a value
 * that is not a date.
 */
export const formatMeetingStart = (iso, { locale = undefined, timeZone = undefined } = {}) => {
  const when = new Date(iso);
  if (!iso || Number.isNaN(when.getTime())) return '';
  return new Intl.DateTimeFormat(locale, {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
    timeZoneName: 'short',
    timeZone,
  }).format(when);
};

/** A length of time as "m:ss" under an hour and "h:mm:ss" from an hour. */
export const formatDuration = (millis) => {
  const total = Math.max(0, Math.floor((Number(millis) || 0) / 1000));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = String(total % 60).padStart(2, '0');
  return h ? `${h}:${String(m).padStart(2, '0')}:${s}` : `${m}:${s}`;
};

/** A length of time as "HH:MM:SS", the form the summary download has always used. */
export const formatDurationClock = (millis) => {
  const total = Math.max(0, Math.floor((Number(millis) || 0) / 1000));
  const pad = (n) => String(n).padStart(2, '0');
  return `${pad(Math.floor(total / 3600))}:${pad(Math.floor((total % 3600) / 60))}:${pad(total % 60)}`;
};

// The channels whose times are on the meeting's clock. Assistant answers carry
// the time of the line that asked; chat messages carry a wall-clock time.
const SPEECH_CHANNELS = ['AGENT', 'CALLER'];

/** How far into the meeting its transcript reaches: the latest end of a final line, in milliseconds. */
export const transcriptExtentMillis = (segments) =>
  (segments || []).reduce((latest, segment) => {
    if (!segment || segment.isPartial || !SPEECH_CHANNELS.includes(segment.channel)) return latest;
    const end = Number(segment.endTime);
    return Number.isFinite(end) && end * 1000 > latest ? end * 1000 : latest;
  }, 0);

/**
 * The meeting's length, in milliseconds.
 *
 * The stored TotalConversationDurationMillis is the end time of whichever final
 * transcript line the event processor wrote last, not the latest one, so a line
 * written late with an earlier time leaves it short. The transcript the page
 * has loaded is read as well, and the longer of the two is shown.
 */
export const meetingDurationMillis = (item, segments) =>
  Math.max(Number(item?.totalConversationDurationMillis) || 0, transcriptExtentMillis(segments));

const PLATFORM_LABELS = {
  ZOOM: 'Zoom',
  TEAMS: 'Microsoft Teams',
  CHIME: 'Amazon Chime',
  WEBEX: 'Webex',
  GOOGLE_MEET: 'Google Meet',
  MEET: 'Google Meet',
};

/** A Virtual Participant's meetingPlatform as people write it. */
export const platformLabel = (platform) => {
  if (!platform || typeof platform !== 'string') return '';
  return PLATFORM_LABELS[platform.toUpperCase()] || platform;
};
