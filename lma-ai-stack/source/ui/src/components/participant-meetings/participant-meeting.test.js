/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import { describe, it, expect, vi } from 'vitest';

import {
  participantConsentState,
  participantMeetingItem,
  participantTranscript,
  recordingKey,
  recordingLinks,
} from './participant-meeting';
import { playbackSessions } from '../meeting-player/playback-sync';
import { IN_PROGRESS_STATUS } from '../common/get-recording-status';

const MEETING = {
  CallId: 'Weekly sync',
  CreatedAt: '2026-11-24T10:00:00.000Z',
  Status: 'TRANSCRIBING',
  Owner: 'owner@example.com',
  SummaryStatus: 'running',
  OtherParticipantsName: 'Others',
  Sessions: [{ SessionNumber: 2, OffsetSeconds: 600 }],
};
const link = (Kind, SessionNumber, Url) => ({ Kind, SessionNumber, Url, ExpiresInSeconds: 3600 });

describe('a shared meeting as the meeting page holds it', () => {
  it('maps the Participant view onto the meeting page’s fields', () => {
    const item = participantMeetingItem(MEETING);
    expect(item.callId).toBe('Weekly sync');
    expect(item.owner).toBe('owner@example.com');
    expect(item.recordingStatusLabel).toBe(IN_PROGRESS_STATUS);
    expect(JSON.parse(item.callSummaryState)).toEqual({ status: 'running' });
    expect(item.remoteParticipantName).toBe('Others');
    expect(item.recordingUrl).toBeNull();
  });

  it('names each recording by kind and session, as the player plays a continued meeting', () => {
    const item = participantMeetingItem({ ...MEETING, Status: 'ENDED' }, [
      link('AUDIO', 1, 'https://s/a1?sig'),
      link('AUDIO', 2, 'https://s/a2?sig'),
      link('VIDEO', 2, 'https://s/v2?sig'),
    ]);
    expect(item.recordingUrl).toBe('participant-recording:AUDIO:2');
    expect(item.videoRecordingUrl).toBe('participant-recording:VIDEO:2');
    const sessions = playbackSessions(item);
    expect(sessions.map((s) => [s.number, s.offset, s.recordingUrl, s.videoRecordingUrl])).toEqual([
      [1, 0, 'participant-recording:AUDIO:1', null],
      [2, 600, 'participant-recording:AUDIO:2', 'participant-recording:VIDEO:2'],
    ]);
  });

  it('groups the transcript by channel', () => {
    const transcript = participantTranscript('Weekly sync', [
      { SegmentId: 's1', Channel: 'REMOTE', Transcript: 'hi', StartTime: 1, EndTime: 2 },
      { SegmentId: 'c1', Channel: 'MEETING_CHAT', Transcript: 'link', StartTime: 3, EndTime: 3 },
    ]);
    expect(Object.keys(transcript['Weekly sync']).sort()).toEqual(['MEETING_CHAT', 'REMOTE']);
    expect(transcript['Weekly sync'].REMOTE.segments[0]).toMatchObject({ callId: 'Weekly sync', segmentId: 's1' });
  });

  it('gives the consent record’s replies as its events', () => {
    expect(participantConsentState(null).consent).toBeNull();
    const replies = [{ ParticipantName: 'Olga', Decision: 'consent' }];
    expect(participantConsentState({ Mode: 'opt_in', Replies: replies }).consent.Events).toEqual(replies);
  });
});

describe('the recording links', () => {
  it('serves the player from the page’s read, and reads again once it is old', async () => {
    let now = 1000;
    const fetchLinks = vi
      .fn()
      .mockResolvedValueOnce([link('AUDIO', 1, 'https://s/a1?first')])
      .mockResolvedValueOnce([link('AUDIO', 1, 'https://s/a1?second')]);
    const links = recordingLinks(fetchLinks, { now: () => now });
    expect(await links.load()).toHaveLength(1);
    expect(await links.sign(recordingKey(link('AUDIO', 1)))).toBe('https://s/a1?first');
    expect(fetchLinks).toHaveBeenCalledTimes(1);
    now += 60000;
    expect(await links.sign('participant-recording:AUDIO:1')).toBe('https://s/a1?second');
    expect(fetchLinks).toHaveBeenCalledTimes(2);
  });

  it('refuses a recording the server no longer gives, and reads again after a failed read', async () => {
    const fetchLinks = vi.fn().mockRejectedValueOnce(new Error('throttled')).mockResolvedValueOnce([]);
    const links = recordingLinks(fetchLinks);
    await expect(links.load()).rejects.toThrow('throttled');
    await expect(links.sign('participant-recording:VIDEO:latest')).rejects.toThrow('no longer available');
    expect(fetchLinks).toHaveBeenCalledTimes(2);
  });
});
