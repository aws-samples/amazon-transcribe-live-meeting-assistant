/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The transcript of a meeting shared with a Participant, as their page reads it.
 *
 * Pure: the page reads the stored lines a page at a time
 * (getParticipantMeetingTranscript) and receives the live ones
 * (onAddParticipantTranscriptSegment); these helpers merge the two and order
 * them. The meeting page then names each line's speaker through the meeting's
 * name map, as it does for anyone (participant-meeting.js). The server sends
 * only what was said and what was typed in the meeting chat.
 */
import { isChatChannel, isSpeechChannel } from '../common/channels';

const seconds = (value) => (typeof value === 'number' && Number.isFinite(value) ? value : 0);

/**
 * The lines with one more merged in: a line replaces the one with its SegmentId
 * (a partial line is replaced by its final form, never the other way round), and
 * the result is in the order the lines were spoken.
 */
export const mergeSegment = (lines, segment) => {
  if (!segment || !segment.SegmentId) return lines;
  if (!isSpeechChannel(segment.Channel) && !isChatChannel(segment.Channel)) return lines;
  const index = lines.findIndex((line) => line.SegmentId === segment.SegmentId);
  if (index >= 0 && lines[index].IsPartial === false && segment.IsPartial) return lines;
  const next = index >= 0 ? [...lines.slice(0, index), segment, ...lines.slice(index + 1)] : [...lines, segment];
  return next.sort((a, b) => seconds(a.StartTime) - seconds(b.StartTime) || seconds(a.EndTime) - seconds(b.EndTime));
};

/** Several lines merged in, as mergeSegment merges one. */
export const mergeSegments = (lines, segments) => (segments || []).reduce(mergeSegment, lines);

/** "m:ss" or "h:mm:ss" from the start of the meeting. */
export const formatOffset = (value) => {
  const total = Math.max(0, Math.floor(seconds(value)));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = `${total % 60}`.padStart(2, '0');
  return h > 0 ? `${h}:${`${m}`.padStart(2, '0')}:${s}` : `${m}:${s}`;
};

/** A meeting's length from its TotalConversationDurationMillis, or '' when it has none. */
export const formatDuration = (millis) => (typeof millis === 'number' && millis > 0 ? formatOffset(millis / 1000) : '');
