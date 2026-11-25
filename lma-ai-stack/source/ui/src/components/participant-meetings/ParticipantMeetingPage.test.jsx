/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The read-only page of a meeting shared with a Participant: the meeting page
 * (CallPanel) in its Participant mode, fed by the Participant operations only.
 * What it asks the API for, that it makes no other request, what it shows --
 * the transcript with Auto-scroll, the meeting assistant beside the tabs with
 * the Participant chat buttons, the recordings through the links the server
 * signs -- and that it renders none of the owner's panels or controls.
 *
 * The page's own client is injected; every other client (the one the meeting
 * page and its hooks would use for the owner's queries) is a recorder that
 * fails the test if asked anything.
 */
import React from 'react';
import { describe, it, expect, vi, afterEach, beforeAll, beforeEach } from 'vitest';
import { act, render, screen, cleanup, waitFor, fireEvent, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

const ownerRequests = vi.hoisted(() => []);

vi.mock('aws-amplify/api', () => ({
  generateClient: () => ({
    graphql: (request) => {
      ownerRequests.push(request.query);
      const result = Promise.resolve({ data: {} });
      result.subscribe = () => ({ unsubscribe: () => undefined });
      return result;
    },
  }),
}));
vi.mock('../../contexts/app', () => ({
  default: () => ({ currentCredentials: {}, user: { username: 'pat@example.com' }, setErrorMessage: vi.fn() }),
}));
vi.mock('../../hooks/use-user-groups', () => ({
  default: () => ({ userGroups: ['Participant'], isAdmin: false, role: 'Participant', isLoading: false }),
}));

/* eslint-disable import/first */
import ParticipantMeetingPage from './ParticipantMeetingPage';
import ParticipantMeetingList, { sharedMeetingHref } from './ParticipantMeetingList';
import * as queries from '../../graphql/queries/participantMeetings';
import { PARTICIPANT_CHAT_BUTTONS } from '../../hooks/use-strands-chat-bridge';
import { installMediaElementStub } from '../meeting-player/media-element-stub';
/* eslint-enable import/first */

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
const ENDED = { ...MEETING, Status: 'ENDED' };

const SEGMENT = {
  CallId: 'Weekly sync',
  SegmentId: 's1',
  StartTime: 3,
  EndTime: 4,
  Transcript: 'Hello everyone',
  IsPartial: false,
  Channel: 'REMOTE',
  Speaker: 'spk_0',
  CreatedAt: '2026-11-24T10:00:03.000Z',
};
const CHAT = {
  ...SEGMENT,
  SegmentId: 'c1',
  StartTime: 5,
  EndTime: 5,
  Channel: 'MEETING_CHAT',
  Speaker: 'Ben',
  Transcript: 'Agenda link in a minute',
  CreatedAt: '2026-11-24T10:00:05.000Z',
};

const LINK = 'https://bucket.example/a.wav?sig';

const fakeClient = ({ meeting = MEETING, refuse = false, links = [LINK] } = {}) => {
  const listeners = [];
  let linkIndex = 0;
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
      return Promise.resolve({
        data: { getParticipantMeetingTranscript: { Segments: [SEGMENT, CHAT], nextToken: null } },
      });
    }
    if (request.query === queries.getParticipantMeetingRecordings) {
      const url = links[Math.min(linkIndex, links.length - 1)];
      linkIndex += 1;
      return Promise.resolve({
        data: {
          getParticipantMeetingRecordings: [{ Kind: 'AUDIO', SessionNumber: 1, Url: url, ExpiresInSeconds: 3600 }],
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

const openTab = (name) => fireEvent.click(screen.getByRole('tab', { name }));

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

let pageWidth;
beforeAll(installMediaElementStub);
beforeEach(() => {
  ownerRequests.length = 0;
  vi.stubEnv('VITE_ENABLE_AGENT_ASSIST', 'true');
  window.localStorage.clear();
  // jsdom lays nothing out: a page wide enough for the assistant beside the tabs.
  pageWidth = vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockReturnValue(1200);
  Element.prototype.scrollTo = vi.fn();
  Element.prototype.scrollIntoView = vi.fn();
});
afterEach(() => {
  cleanup();
  vi.unstubAllEnvs();
  pageWidth.mockRestore();
});

describe('the Participant meeting page', () => {
  it('asks the API for no field of the owner’s own material', () => {
    const documents = Object.values(queries).join('\n');
    OWNER_ONLY_FIELDS.forEach((field) => {
      expect(documents).not.toMatch(new RegExp(`\\b${field}\\b`));
    });
  });

  it('is the meeting page: its header, its tabs and the transcript with Auto-scroll on while live', async () => {
    renderPage(fakeClient());
    expect(await screen.findByText('Hello everyone')).toBeTruthy();
    expect(screen.getByTestId('meeting-title').textContent).toBe('Weekly sync');
    expect(screen.getAllByRole('tab').map((tab) => tab.textContent)).toEqual(['Summary', 'Transcript', 'Details']);
    // The speaker through the owner's name map, and the chat line by its sender.
    expect(screen.getAllByText('Olga').length).toBeGreaterThan(0);
    expect(screen.getByText('Agenda link in a minute')).toBeTruthy();
    const autoScroll = screen.getByRole('checkbox', { name: 'Auto-scroll' });
    expect(autoScroll.checked).toBe(true);
    expect(autoScroll.disabled).toBe(false);
  });

  it('follows the newest line as live lines arrive', async () => {
    const client = fakeClient();
    renderPage(client);
    await screen.findByText('Hello everyone');
    await waitFor(() => expect(client.listeners.length).toBe(1));
    const scroller = screen.getByTestId('transcript-scroll');
    scroller.scrollTo.mockClear?.();
    const scrolled = vi.spyOn(scroller, 'scrollTo');
    act(() => {
      client.listeners[0].next({
        data: {
          onAddParticipantTranscriptSegment: {
            ...SEGMENT,
            SegmentId: 's2',
            StartTime: 9,
            EndTime: 10,
            Transcript: 'Next item',
            CreatedAt: '2026-11-24T10:00:09.000Z',
          },
        },
      });
    });
    expect(await screen.findByText('Next item')).toBeTruthy();
    await waitFor(() => expect(scrolled).toHaveBeenCalled());
    expect(scrolled.mock.calls.at(-1)[0]).toMatchObject({ top: scroller.scrollHeight });
  });

  it('has the meeting assistant open in the panel beside the tabs, with the Participant chat buttons', async () => {
    renderPage(fakeClient());
    await screen.findByText('Hello everyone');
    const panel = document.getElementById('meeting-assistant-panel');
    expect(panel).toBeTruthy();
    expect(panel.hidden).toBe(false);
    expect(panel.querySelector('iframe[src*="strands-chat.html"]')).toBeTruthy();
    expect(screen.getByRole('button', { name: /^Assistant/ })).toBeTruthy();
    // The panel lies beside the tabs, in the meeting page's layout.
    expect(panel.parentElement.className).toContain('meeting-body');
    expect(panel.parentElement.className).toContain('meeting-body-assistant-open');

    const source = { postMessage: vi.fn() };
    window.dispatchEvent(
      new MessageEvent('message', {
        origin: window.location.origin,
        source,
        data: { type: 'STRANDS_BUTTON_CONFIG_REQUEST', requestId: 'r1' },
      }),
    );
    await waitFor(() => expect(source.postMessage).toHaveBeenCalled());
    expect(source.postMessage.mock.calls[0][0].config).toEqual(PARTICIPANT_CHAT_BUTTONS);
  });

  it('shows the summary, agenda, participants and consent record', async () => {
    renderPage(fakeClient());
    await screen.findByText('Hello everyone');
    openTab('Summary');
    expect(screen.getByText('Send the deck')).toBeTruthy();
    expect(screen.getByText('We agreed')).toBeTruthy();
    expect(screen.getAllByText(/Budget/).length).toBeGreaterThan(0);
    openTab('Details');
    const names = screen.getByRole('list', { name: 'Participants' });
    expect(
      within(names)
        .getAllByRole('listitem')
        .map((li) => li.textContent),
    ).toEqual(['Pat', 'Olga']);
    expect(screen.getByText('This meeting is recorded.')).toBeTruthy();
  });

  it('renders none of the owner’s panels or controls, and makes no request but the Participant ones', async () => {
    renderPage(fakeClient({ meeting: ENDED }));
    await screen.findByText('Hello everyone');
    ['Share', 'Download', 'More actions', 'Diagnose', 'Name speakers', 'Edit', 'Regenerate', 'Add agenda'].forEach(
      (name) => expect(screen.queryByRole('button', { name: new RegExp(`^${name}`) })).toBeNull(),
    );
    ['Record an objection', 'Attest that everyone agreed'].forEach((name) =>
      expect(screen.queryByRole('button', { name })).toBeNull(),
    );
    expect(screen.queryByRole('checkbox', { name: 'Translate' })).toBeNull();
    expect(screen.queryByRole('tab', { name: /Whiteboards|Screen share/ })).toBeNull();
    expect(screen.queryByTestId('meeting-cost-chip')).toBeNull();
    ['Meeting context', 'Checklist', 'Live guidance', 'Meeting attributes', 'Capture and processing'].forEach(
      (heading) => expect(screen.queryByText(heading)).toBeNull(),
    );
    // Let every effect run, then: not one request through any other client.
    await waitFor(() => expect(document.querySelector('audio, video')).toBeTruthy());
    expect(ownerRequests).toEqual([]);
  });

  it('does not subscribe to a meeting that has ended', async () => {
    const client = fakeClient({ meeting: ENDED });
    renderPage(client);
    await screen.findByText('Hello everyone');
    expect(client.listeners.length).toBe(0);
  });

  it('says a meeting not shared with the reader is not theirs to read', async () => {
    renderPage(fakeClient({ refuse: true }));
    expect(await screen.findByText('Access denied')).toBeTruthy();
    expect(screen.queryByText('Transcript')).toBeNull();
  });
});

describe('recording playback', () => {
  const recordingCalls = (client) =>
    client.graphql.mock.calls.filter((call) => call[0].query === queries.getParticipantMeetingRecordings).length;

  it('plays the recording through the link the server signs, in the meeting page’s player', async () => {
    const client = fakeClient({ meeting: ENDED });
    renderPage(client);
    await waitFor(() => expect(document.querySelector('audio')?.getAttribute('src')).toBe(LINK));
    // The page's read serves the player's first request.
    expect(recordingCalls(client)).toBe(1);
    // A line of the transcript plays the recording from its start.
    expect(screen.getAllByRole('button', { name: /^Play from / }).length).toBeGreaterThan(0);
  });

  it('asks for a fresh link when the one it has stops working', async () => {
    const fresh = 'https://bucket.example/a.wav?fresh';
    const client = fakeClient({ meeting: ENDED, links: [LINK, fresh] });
    const now = vi.spyOn(Date, 'now');
    renderPage(client);
    await waitFor(() => expect(document.querySelector('audio')?.getAttribute('src')).toBe(LINK));
    // More than a minute later, past the reuse of the page's read.
    now.mockReturnValue(Date.now() + 120000);
    fireEvent.error(document.querySelector('audio'));
    await waitFor(() => expect(document.querySelector('audio')?.getAttribute('src')).toBe(fresh));
    expect(recordingCalls(client)).toBe(2);
    now.mockRestore();
  });

  it('reads no recording links while the meeting is live', async () => {
    const client = fakeClient();
    renderPage(client);
    await screen.findByText('Hello everyone');
    expect(recordingCalls(client)).toBe(0);
    expect(document.querySelector('audio, video')).toBeNull();
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
