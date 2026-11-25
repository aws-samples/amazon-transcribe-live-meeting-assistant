/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The read-only page of a meeting shared with a Participant: what it asks the
 * API for, what it shows, and that it offers nothing that changes the meeting.
 */
import React from 'react';
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, cleanup, waitFor, fireEvent } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

vi.mock('../../hooks/use-strands-chat-bridge', () => ({ default: () => {} }));

/* eslint-disable import/first */
import ParticipantMeetingPage from './ParticipantMeetingPage';
import ParticipantMeetingList, { sharedMeetingHref } from './ParticipantMeetingList';
import * as queries from '../../graphql/queries/participantMeetings';
/* eslint-enable import/first */

afterEach(cleanup);

const MEETING = {
  CallId: 'Weekly sync',
  CreatedAt: '2026-11-24T10:00:00.000Z',
  Status: 'TRANSCRIBING',
  Owner: 'owner@example.com',
  CallSummaryText: '**We agreed** the plan.',
  ActionItems: [{ Text: 'Send the deck', Owner: 'Olga' }],
  Agenda: 'Intro\nBudget',
  ParticipantNames: ['Pat', 'Olga'],
  SpeakerIdentities: JSON.stringify({ identities: { 'REMOTE#spk_0': { name: 'Olga' } } }),
  Consent: {
    Mode: 'notify',
    Status: 'not_required',
    CaptureAllowed: true,
    NoticeText: 'This meeting is recorded.',
    Replies: [{ ParticipantName: 'Olga', Decision: 'consent', Source: 'chat' }],
  },
  HasAudioRecording: true,
  HasVideoRecording: false,
};

const SEGMENT = {
  CallId: 'Weekly sync',
  SegmentId: 's1',
  StartTime: 3,
  EndTime: 4,
  Transcript: 'Hello everyone',
  IsPartial: false,
  Channel: 'REMOTE',
  Speaker: 'spk_0',
};

const fakeClient = ({ meeting = MEETING, refuse = false } = {}) => {
  const listeners = [];
  const graphql = vi.fn((request) => {
    if (request.query === queries.onAddParticipantTranscriptSegment) {
      return {
        subscribe: (observer) => {
          listeners.push(observer);
          return { unsubscribe: vi.fn() };
        },
      };
    }
    if (request.query === queries.getParticipantMeeting) {
      if (refuse) return Promise.reject(new Error('Unauthorized: not authorized to read this meeting'));
      return Promise.resolve({ data: { getParticipantMeeting: meeting } });
    }
    if (request.query === queries.getParticipantMeetingTranscript) {
      return Promise.resolve({ data: { getParticipantMeetingTranscript: { Segments: [SEGMENT], nextToken: null } } });
    }
    if (request.query === queries.getParticipantMeetingRecordings) {
      return Promise.resolve({
        data: {
          getParticipantMeetingRecordings: [
            { Kind: 'AUDIO', SessionNumber: 1, Url: 'https://bucket.example/a.wav?sig', ExpiresInSeconds: 3600 },
          ],
        },
      });
    }
    if (request.query === queries.listParticipantMeetings) {
      return Promise.resolve({ data: { listParticipantMeetings: { Meetings: [MEETING], nextToken: null } } });
    }
    return Promise.reject(new Error(`unexpected query ${request.query}`));
  });
  return { graphql, listeners };
};

const renderPage = (client) =>
  render(
    <MemoryRouter>
      <ParticipantMeetingPage client={client} callId="Weekly sync" />
    </MemoryRouter>,
  );

// The owner's own material and the meeting's controls: none of it is asked for or offered.
const OWNER_ONLY_FIELDS = [
  'MeetingContext',
  'Checklist',
  'Labels',
  'Audience',
  'SummaryProfile',
  'GuidanceEnabled',
  'WhiteboardsEnabled',
  'SharedWith',
  'AutoSharedWith',
  'RecordingUrl',
  'VideoRecordingUrl',
  'Metadatajson',
  'Sentiment',
  'Attestation',
  'Evidence',
];

describe('the Participant meeting page', () => {
  it('asks the API for no field of the owner’s own material', () => {
    const documents = Object.values(queries).join('\n');
    OWNER_ONLY_FIELDS.forEach((field) => {
      expect(documents).not.toMatch(new RegExp(`\\b${field}\\b`));
    });
  });

  it('shows the summary, agenda, transcript, participants and consent, and no control that changes them', async () => {
    const client = fakeClient();
    renderPage(client);
    expect(await screen.findByText('Send the deck')).toBeTruthy();
    expect(screen.getByText('We agreed')).toBeTruthy();
    expect(screen.getByText(/Intro/)).toBeTruthy();
    expect(screen.getByText('Pat, Olga')).toBeTruthy();
    expect(screen.getByText('This meeting is recorded.')).toBeTruthy();
    expect(await screen.findByText('Hello everyone')).toBeTruthy();
    expect(screen.getByText('Olga:')).toBeTruthy();
    ['Edit', 'Delete', 'Share', 'Regenerate', 'Save', 'Derive agenda', 'Unshare'].forEach((name) => {
      expect(screen.queryByRole('button', { name: new RegExp(`^${name}`, 'i') })).toBeNull();
    });
  });

  it('adds live lines while the meeting is in progress', async () => {
    const client = fakeClient();
    renderPage(client);
    await screen.findByText('Hello everyone');
    await waitFor(() => expect(client.listeners.length).toBe(1));
    client.listeners[0].next({
      data: {
        onAddParticipantTranscriptSegment: { ...SEGMENT, SegmentId: 's2', StartTime: 9, Transcript: 'Next item' },
      },
    });
    expect(await screen.findByText('Next item')).toBeTruthy();
  });

  it('does not subscribe to a meeting that has ended', async () => {
    const client = fakeClient({ meeting: { ...MEETING, Status: 'ENDED' } });
    renderPage(client);
    await screen.findByText('Hello everyone');
    expect(client.listeners.length).toBe(0);
  });

  it('plays recordings through the links the server signs', async () => {
    const client = fakeClient();
    const { container } = renderPage(client);
    fireEvent.click(await screen.findByRole('button', { name: 'Play recordings' }));
    await waitFor(() => expect(container.querySelector('audio')).toBeTruthy());
    expect(container.querySelector('audio').getAttribute('src')).toBe('https://bucket.example/a.wav?sig');
  });

  it('says a meeting not shared with the reader is not theirs to read', async () => {
    renderPage(fakeClient({ refuse: true }));
    expect(await screen.findByText('Access denied')).toBeTruthy();
    expect(screen.queryByText('Transcript')).toBeNull();
  });
});

describe('the Participant meetings list', () => {
  it('lists the meetings shared with the reader, each linking to its page', async () => {
    render(
      <MemoryRouter>
        <ParticipantMeetingList client={fakeClient()} />
      </MemoryRouter>,
    );
    const link = await screen.findByRole('link', { name: 'Weekly sync' });
    expect(link.getAttribute('href')).toBe(sharedMeetingHref('Weekly sync'));
    expect(sharedMeetingHref('a b+c')).toBe('#/shared-meetings/a%20b%2Bc');
    expect(screen.getByText('owner@example.com')).toBeTruthy();
  });

  it('keeps reading while a page comes back empty with more to read', async () => {
    const pages = [
      { Meetings: [], nextToken: 't1' },
      { Meetings: [], nextToken: 't2' },
      { Meetings: [MEETING], nextToken: null },
    ];
    const graphql = vi.fn(async () => ({ data: { listParticipantMeetings: pages.shift() } }));
    render(
      <MemoryRouter>
        <ParticipantMeetingList client={{ graphql }} />
      </MemoryRouter>,
    );
    expect(await screen.findByRole('link', { name: 'Weekly sync' })).toBeTruthy();
    expect(graphql.mock.calls.map((call) => call[0].variables.nextToken)).toEqual([null, 't1', 't2']);
  });

  it('says older meetings remain rather than that none is shared', async () => {
    const graphql = vi.fn(async () => ({ data: { listParticipantMeetings: { Meetings: [], nextToken: 'more' } } }));
    render(
      <MemoryRouter>
        <ParticipantMeetingList client={{ graphql }} />
      </MemoryRouter>,
    );
    expect(await screen.findByText(/Choose Show more to search older ones/)).toBeTruthy();
    expect(screen.queryByText('No meeting has been shared with you yet.')).toBeNull();
    expect(graphql).toHaveBeenCalledTimes(10);
  });
});

describe('recording playback', () => {
  it('asks for fresh links once after a player error, then shows the error', async () => {
    const client = fakeClient();
    const { container } = renderPage(client);
    fireEvent.click(await screen.findByRole('button', { name: 'Play recordings' }));
    await waitFor(() => expect(container.querySelector('audio')).toBeTruthy());
    const recordingCalls = () =>
      client.graphql.mock.calls.filter((call) => call[0].query === queries.getParticipantMeetingRecordings).length;
    fireEvent.error(container.querySelector('audio'));
    await waitFor(() => expect(recordingCalls()).toBe(2));
    fireEvent.error(container.querySelector('audio'));
    expect(await screen.findByText('The recording could not be played. Try again later.')).toBeTruthy();
    expect(recordingCalls()).toBe(2);
  });
});
