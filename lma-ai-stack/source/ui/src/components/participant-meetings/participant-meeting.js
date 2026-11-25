/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * A meeting shared with a Participant, in the form the meeting page
 * (call-panel/CallPanel.jsx) shows a meeting in.
 *
 * Pure. The Participant's page reads the meeting, its transcript and its
 * recording links through the Participant operations
 * (graphql/queries/participantMeetings.js); these helpers turn what they
 * return into the mapped meeting, the transcript by channel and the consent
 * state the meeting page's components read, so that the Participant's page is
 * the meeting page in its Participant mode rather than a page of its own.
 */
import mapCallsAttributes from '../common/map-call-attributes';
import { mapTranscriptSegmentValue } from '../../hooks/use-calls-graphql-api';

/**
 * The name the page gives one recording, as the player's link for it: the
 * kind and the capture session. The player asks the signer for a playable
 * link by this name (recordingLinks), so a link that has lapsed is replaced by
 * a fresh one without the recording changing on screen.
 */
export const recordingKey = (link) => `participant-recording:${link.Kind}:${link.SessionNumber ?? 'latest'}`;

const keysOf = (links, kind) => links.filter((link) => link?.Kind === kind && link.Url);

/**
 * The meeting as the meeting page holds one (mapCallsAttributes), from the
 * Participant view of it and its recording links (getParticipantMeetingRecordings,
 * or none). Each recording is named by recordingKey, the latest of each kind
 * as the meeting's recording and every one with its session, as the player
 * reads a meeting's sessions (meeting-player/playback-sync.js).
 */
export const participantMeetingItem = (meeting, links = []) => {
  const audio = keysOf(links || [], 'AUDIO');
  const video = keysOf(links || [], 'VIDEO');
  const [item] = mapCallsAttributes([
    {
      CallId: meeting.CallId,
      CreatedAt: meeting.CreatedAt || meeting.UpdatedAt || new Date(0).toISOString(),
      UpdatedAt: meeting.UpdatedAt,
      Status: meeting.Status,
      TotalConversationDurationMillis: meeting.TotalConversationDurationMillis || 0,
      Owner: meeting.Owner,
      MeetingPlatform: meeting.MeetingPlatform,
      Agenda: meeting.Agenda,
      CallSummaryText: meeting.CallSummaryText,
      // The view gives the summary's status alone; the page reads it from the state.
      CallSummaryState: meeting.SummaryStatus ? JSON.stringify({ status: meeting.SummaryStatus }) : undefined,
      ActionItems: meeting.ActionItems || [],
      SpeakerIdentities: meeting.SpeakerIdentities,
      CustomerPhoneNumber: meeting.OtherParticipantsName,
      Sessions: meeting.Sessions || [],
      TranscriptDeletedAt: meeting.TranscriptDeletedAt,
      RecordingsDeletedAt: meeting.RecordingsDeletedAt,
      RecordingUrl: audio.length ? recordingKey(audio[audio.length - 1]) : null,
      RecordingUrls: audio.map(recordingKey),
      RecordingUrlSessions: audio.map((link) => link.SessionNumber ?? null),
      VideoRecordingUrl: video.length ? recordingKey(video[video.length - 1]) : null,
      VideoRecordingUrls: video.map(recordingKey),
      VideoRecordingUrlSessions: video.map((link) => link.SessionNumber ?? null),
    },
  ]);
  return item;
};

/**
 * The transcript as the meeting page holds it: { [callId]: { [channel]: { segments } } },
 * from the Participant view's segments (already merged: mergeSegments).
 */
export const participantTranscript = (callId, segments) => {
  const byChannel = {};
  (segments || []).forEach((raw) => {
    const segment = mapTranscriptSegmentValue({ ...raw, CallId: raw.CallId || callId });
    const entry = byChannel[segment.channel] || { segments: [] };
    entry.segments.push(segment);
    byChannel[segment.channel] = entry;
  });
  return { [callId]: byChannel };
};

/**
 * The consent state the meeting page's consent components read
 * (useMeetingConsent's shape), from the Participant view's record: its
 * replies are the record's events. Nothing in it can change the record.
 */
export const participantConsentState = (consent) => ({
  consent: consent ? { ...consent, Events: consent.Replies || [] } : null,
  error: null,
  loading: false,
});

/** How long a read of the recording links is reused, for the player asking for each recording in turn. */
export const LINKS_REUSE_MS = 30000;

/**
 * The recording links of one meeting, read through `fetchLinks` (the
 * getParticipantMeetingRecordings query): `load()` for the page, and
 * `sign(key)` for the player, which asks again when a link it was given stops
 * working (MeetingPlayer re-signs at most once a minute). A read is reused for
 * LINKS_REUSE_MS, so the page's read serves the player's first request.
 */
export const recordingLinks = (fetchLinks, { now = () => Date.now(), reuseMs = LINKS_REUSE_MS } = {}) => {
  let last = null;
  const load = async () => {
    if (!last || now() - last.at > reuseMs) last = { at: now(), links: fetchLinks() };
    const { links } = last;
    try {
      return (await links) || [];
    } catch (error) {
      if (last?.links === links) last = null;
      throw error;
    }
  };
  const sign = async (key) => {
    const link = (await load()).find((candidate) => recordingKey(candidate) === key);
    if (!link) throw new Error('The recording is no longer available.');
    return link.Url;
  };
  return { load, sign };
};
