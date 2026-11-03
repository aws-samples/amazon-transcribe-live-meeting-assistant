/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The meeting page as a whole: the header above the tabs, which tab is shown
 * (and that the choice lives in the URL), the Screen share tab appearing once
 * the meeting has key frames, and the Overview's summary showing the slides as
 * links and the chapters once.
 *
 * Everything that talks to AWS is replaced: the GraphQL client answers the
 * handful of queries the page makes, and the contexts supply a signed-in user.
 */
import React from 'react';
import { describe, it, expect, vi, beforeAll, beforeEach } from 'vitest';
import { act, render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { MemoryRouter, useLocation } from 'react-router-dom';

const responses = vi.hoisted(() => ({ frames: [], cost: null, vps: [] }));

vi.mock('aws-amplify/api', () => {
  const answer = ({ query }) => {
    if (query.includes('listVirtualParticipants')) return { data: { listVirtualParticipants: responses.vps } };
    if (query.includes('getVideoKeyframes')) {
      return { data: { getVideoKeyframes: { VideoKeyframes: responses.frames, nextToken: null } } };
    }
    if (query.includes('getCallCost')) return { data: { getCallCost: responses.cost } };
    return { data: {} };
  };
  const graphql = (options) => {
    const result = Promise.resolve(answer(options));
    result.subscribe = () => ({ unsubscribe: () => undefined });
    return result;
  };
  return { generateClient: () => ({ graphql }) };
});
vi.mock('../../contexts/app', () => ({
  default: () => ({ currentCredentials: {}, user: { username: 'alice' }, setErrorMessage: vi.fn() }),
}));
vi.mock('../../contexts/settings', () => ({ default: () => ({ settings: {} }) }));
vi.mock('../../contexts/calls', () => ({ default: () => ({ calls: [] }) }));
vi.mock('../../hooks/use-user-groups', () => ({ default: () => ({ isAdmin: false, isLoading: false }) }));
vi.mock('../../hooks/use-meeting-consent', () => ({ default: () => ({ consent: null }) }));
vi.mock('../../hooks/use-strands-chat-bridge', () => ({ default: () => undefined }));
vi.mock('../virtual-participant-layout/VNCViewer', () => ({ default: () => <div data-testid="vnc" /> }));
vi.mock('../mcp-servers', () => ({ default: () => null }));
vi.mock('../common/generate-s3-presigned-url', () => ({
  default: vi.fn().mockResolvedValue('https://signed.example/x'),
}));

// eslint-disable-next-line import/first
import { CallPanel } from './CallPanel';
// eslint-disable-next-line import/first
import { DONE_STATUS, IN_PROGRESS_STATUS } from '../common/get-recording-status';
// eslint-disable-next-line import/first
import { installMediaElementStub, loadMedia } from '../meeting-player/media-element-stub';

/* eslint-disable react/prop-types */

const SUMMARY = JSON.stringify({
  SUMMARY: 'Sarah walked through the plan.',
  'SLIDES PRESENTED': '**[00:30] Budget**\n\n![Slide at 00:30](keyframe:0000030000-ab12cd)\n\nSarah covered Q3.',
  CHAPTERS: '- [00:00] Welcome\n- [05:12] Budget review',
});

const ITEM = {
  callId: 'Weekly sync-2026-11-03-14-05-09-123',
  callSummaryText: SUMMARY,
  initiationTimeStamp: '2026-11-03T14:05:09.123Z',
  updatedAt: '2026-11-03T14:30:00.000Z',
  totalConversationDurationMillis: 177000,
  conversationDurationTimeStamp: '00:02:57',
  recordingStatusLabel: DONE_STATUS,
  recordingStatusIcon: 'success',
  owner: 'alice',
};

const TRANSCRIPT = {
  [ITEM.callId]: {
    CALLER: {
      segments: [
        {
          segmentId: 's1',
          createdAt: '2026-11-03T14:05:10.000Z',
          channel: 'CALLER',
          speaker: 'Sarah Lee',
          transcript: 'Hello everyone.',
          startTime: 1,
          endTime: 1036,
          isPartial: false,
        },
      ],
    },
  },
};

const frame = (ms) => ({
  CallId: ITEM.callId,
  FrameId: `${String(ms).padStart(10, '0')}-ab12cd`,
  Timestamp: ms / 1000,
  VideoOffset: null,
  ThumbnailSignedUrl: `https://signed.example/${ms}_thumb.jpg`,
  ImageSignedUrl: `https://signed.example/${ms}.jpg`,
});

const LocationProbe = () => {
  const location = useLocation();
  return <div data-testid="location">{`${location.pathname}${location.search}`}</div>;
};

const renderPage = ({ item = ITEM, url = '/calls/m', tabInUrl = true, transcript = TRANSCRIPT } = {}) =>
  render(
    <MemoryRouter initialEntries={[url]}>
      <CallPanel
        item={item}
        callTranscriptPerCallId={transcript}
        setToolsOpen={vi.fn()}
        getCallDetailsFromCallIds={vi.fn()}
        tabInUrl={tabInUrl}
      />
      <LocationProbe />
    </MemoryRouter>,
  );

const selectedTab = () => screen.getAllByRole('tab').find((tab) => tab.getAttribute('aria-selected') === 'true');

beforeAll(installMediaElementStub);

beforeEach(() => {
  // jsdom lays nothing out and has no scrolling.
  Element.prototype.scrollTo = vi.fn();
  Element.prototype.scrollIntoView = vi.fn();
  responses.frames = [];
  responses.cost = null;
  responses.vps = [];
});

describe('the meeting header', () => {
  it('titles the meeting by its name, keeping the full Meeting ID in reach', () => {
    renderPage();
    const title = screen.getByTestId('meeting-title');
    expect(title.textContent).toBe('Weekly sync');
    expect(title.getAttribute('title')).toBe(ITEM.callId);
  });

  it("shows the start in the viewer's time zone, with the UTC value in its tooltip", () => {
    renderPage();
    const start = screen.getByTestId('meeting-start');
    expect(start.textContent).toMatch(/2026/);
    expect(start.textContent).not.toContain('T14:05');
    expect(start.closest('[title]').getAttribute('title')).toBe('2026-11-03T14:05:09.123Z (UTC)');
  });

  it('shows the length the transcript reaches when the stored one is shorter', () => {
    renderPage();
    expect(screen.getByTestId('meeting-duration').textContent).toBe('17:16');
  });

  it('opens the itemised cost estimate from the cost chip', async () => {
    responses.cost = {
      Currency: 'USD',
      TotalCost: 0.29,
      RecordCount: 2,
      UnpricedCount: 0,
      PricesAsOf: '2026-10-01',
      Lines: [],
      NotIncluded: [],
    };
    renderPage();
    await waitFor(() => expect(screen.getByTestId('meeting-cost-chip').textContent).toMatch(/\$0\.29/));
    fireEvent.click(screen.getByTestId('meeting-cost-chip'));
    const popover = await screen.findByRole('dialog', { name: 'Estimated cost' });
    expect(within(popover).getByText('This is an estimate')).toBeTruthy();
    expect(within(popover).getByText(/Total: \$0\.29/)).toBeTruthy();
  });

  it('names the platform when a Virtual Participant joined the meeting', async () => {
    responses.vps = [{ id: 'vp-1', CallId: ITEM.callId, meetingPlatform: 'TEAMS', vncReady: false }];
    renderPage();
    expect((await screen.findAllByText('Microsoft Teams')).length).toBeGreaterThan(0);
  });
});

describe('the tabs', () => {
  it('opens on the transcript when the link names no tab', () => {
    renderPage();
    expect(selectedTab().textContent).toBe('Transcript');
  });

  it('opens on the transcript for a live meeting', () => {
    renderPage({ item: { ...ITEM, recordingStatusLabel: IN_PROGRESS_STATUS, recordingStatusIcon: 'in-progress' } });
    expect(selectedTab().textContent).toBe('Transcript');
  });

  it('opens the tab the link names, and the default for one the meeting does not have', () => {
    const { unmount } = renderPage({ url: '/calls/m?tab=details' });
    expect(selectedTab().textContent).toBe('Details');
    unmount();
    renderPage({ url: '/calls/m?tab=screen-share' });
    expect(selectedTab().textContent).toBe('Transcript');
  });

  it('keeps the selected tab in the URL', () => {
    renderPage({ url: '/calls/m' });
    fireEvent.click(screen.getByRole('tab', { name: 'Overview' }));
    expect(selectedTab().textContent).toBe('Overview');
    expect(screen.getByTestId('location').textContent).toBe('/calls/m?tab=overview');
  });

  it("leaves the host page's URL alone where the panel is embedded", () => {
    renderPage({ url: '/calls', tabInUrl: false });
    fireEvent.click(screen.getByRole('tab', { name: 'Details' }));
    expect(selectedTab().textContent).toBe('Details');
    expect(screen.getByTestId('location').textContent).toBe('/calls');
  });

  it('offers Screen share, with its count, once the meeting has key frames', async () => {
    responses.frames = [frame(30000), frame(60000)];
    renderPage({ url: '/calls/m?tab=screen-share' });
    const tab = await screen.findByRole('tab', { name: 'Screen share (2)' });
    await waitFor(() => expect(selectedTab()).toBe(tab));
    expect(screen.getByTestId('video-timeline')).toBeTruthy();
  });

  it('offers no Screen share tab for a meeting without key frames', async () => {
    renderPage();
    await waitFor(() => expect(screen.getAllByRole('tab')).toHaveLength(3));
    expect(screen.queryByRole('tab', { name: /Screen share/ })).toBeNull();
  });
});

describe('the Overview tab', () => {
  it('shows the slides the summary names as links, not as pictures, and opens one on Screen share', async () => {
    responses.frames = [frame(30000)];
    renderPage({ url: '/calls/m?tab=overview' });
    await screen.findByRole('tab', { name: 'Screen share (1)' });
    const overview = screen.getByText('Sarah walked through the plan.').closest('[role="tabpanel"]');
    expect(within(overview).queryAllByRole('img')).toHaveLength(0);
    fireEvent.click(within(overview).getByRole('button', { name: /Slide at 00:30/ }));
    expect(selectedTab().textContent).toBe('Screen share (1)');
    expect(await screen.findByAltText('Shared screen at 0:30, full size')).toBeTruthy();
  });

  it('lists the chapters once, as the list that moves the transcript', () => {
    renderPage({ url: '/calls/m?tab=overview' });
    expect(screen.getAllByText('Budget review')).toHaveLength(1);
    expect(screen.queryByText('CHAPTERS')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Budget review' }));
    expect(selectedTab().textContent).toBe('Transcript');
  });
});

describe('the player kept in step with the transcript', () => {
  const RECORDED = {
    ...ITEM,
    recordingUrl: 'https://bucket.s3.us-east-1.amazonaws.com/lma-audio-recordings/m.wav',
    videoRecordingUrl: 'https://bucket.s3.us-east-1.amazonaws.com/lma-video-recordings/m.mp4',
  };
  const line = (id, channel, speaker, transcript, startTime, endTime) => ({
    segmentId: id,
    createdAt: `2026-11-03T14:05:${String(startTime % 60).padStart(2, '0')}.000Z`,
    channel,
    speaker,
    transcript,
    startTime,
    endTime,
    isPartial: false,
  });
  const LINES = {
    [ITEM.callId]: {
      REMOTE: {
        segments: [
          line('s1', 'REMOTE', 'Sarah Lee', 'Welcome to the budget review.', 0, 8),
          line('s3', 'REMOTE', 'Sarah Lee', 'The budget is four percent under plan.', 330, 340),
        ],
      },
      LOCAL: { segments: [line('s2', 'LOCAL', 'Bob Chen', 'Thanks, Sarah.', 20, 24)] },
    },
  };
  const keyframe = (seconds, videoOffset) => ({ ...frame(seconds * 1000), VideoOffset: videoOffset });

  const media = () =>
    document.querySelector('[data-testid="meeting-player"] video, [data-testid="meeting-player"] audio');
  const openRecording = async ({ item = RECORDED, ...options } = {}) => {
    const view = renderPage({ item, transcript: LINES, ...options });
    await waitFor(() => expect(media()?.getAttribute('src')).toBe('https://signed.example/x'));
    loadMedia(media(), 1200);
    return view;
  };
  // By the row's whole text: a search's marks split it into pieces.
  const rowOf = (text) => [...document.querySelectorAll('[data-row-id]')].find((row) => row.textContent.includes(text));

  it('is not shown while the meeting is live, and the times are not links', () => {
    renderPage({
      item: { ...RECORDED, recordingStatusLabel: IN_PROGRESS_STATUS, recordingStatusIcon: 'in-progress' },
      transcript: LINES,
    });
    expect(screen.queryByTestId('meeting-player')).toBeNull();
    expect(screen.queryByRole('button', { name: /^Play from/ })).toBeNull();
  });

  it('plays the video on the Transcript tab, and the Details tab has no player of its own', async () => {
    await openRecording();
    expect(media().tagName).toBe('VIDEO');
    expect(media().closest('[role="tabpanel"]')).toBe(screen.getByText('Thanks, Sarah.').closest('[role="tabpanel"]'));
    expect(document.querySelectorAll('video, audio')).toHaveLength(1);
    fireEvent.click(screen.getByRole('tab', { name: 'Details' }));
    expect(screen.getByRole('button', { name: 'Play the video recording' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Open the audio recording file in a new tab' })).toBeTruthy();
  });

  it('plays the audio when that is the only recording', async () => {
    await openRecording({ item: { ...RECORDED, videoRecordingUrl: '' } });
    expect(media().tagName).toBe('AUDIO');
  });

  it("plays from a line's time when it is clicked, and highlights the line", async () => {
    await openRecording();
    fireEvent.click(screen.getByRole('button', { name: 'Play from 00:20.0' }));
    expect(media().currentTime).toBe(20);
    expect(media().paused).toBe(false);
    expect(rowOf('Thanks, Sarah.').getAttribute('aria-current')).toBe('true');
    expect(rowOf('Welcome to the budget review.').getAttribute('aria-current')).toBeNull();
  });

  it('moves the highlight with the recording, on the clock the key frames give', async () => {
    // The video started 30 s before transcription, as a Virtual Participant's does.
    responses.frames = [keyframe(300, 330)];
    await openRecording();
    await screen.findByRole('tab', { name: 'Screen share (1)' });
    fireEvent.click(screen.getByRole('button', { name: 'Play from 05:30.0' }));
    expect(media().currentTime).toBe(360);
    act(() => {
      media().currentTime = 52;
      fireEvent(media(), new Event('timeupdate'));
    });
    expect(rowOf('Thanks, Sarah.').getAttribute('aria-current')).toBe('true');
  });

  it('marks the chapters on the seek bar', async () => {
    await openRecording();
    expect(screen.getAllByTestId('meeting-player-marker').map((m) => m.getAttribute('aria-label'))).toEqual([
      'Chapter at 00:00: Welcome',
      'Chapter at 05:12: Budget review',
    ]);
  });

  it('opens the Transcript tab and plays from a chapter selected on the Overview', async () => {
    await openRecording({ url: '/calls/m?tab=overview' });
    fireEvent.click(screen.getByRole('button', { name: 'Budget review' }));
    expect(selectedTab().textContent).toBe('Transcript');
    expect(media().currentTime).toBe(312);
    expect(media().paused).toBe(false);
  });

  it('moves the player to a frame selected on Screen share without playing, and plays from it on request', async () => {
    responses.frames = [keyframe(300, 330)];
    await openRecording({ url: '/calls/m?tab=screen-share' });
    fireEvent.click(await screen.findByTitle('Key frame at 5:00'));
    expect(media().currentTime).toBe(330);
    expect(media().paused).toBe(true);
    expect(selectedTab().textContent).toBe('Screen share (1)');
    fireEvent.click(screen.getByRole('button', { name: 'Play recording from here' }));
    expect(selectedTab().textContent).toBe('Transcript');
    expect(media().paused).toBe(false);
  });

  it('moves the player to a slide named in the summary', async () => {
    responses.frames = [frame(30000)];
    await openRecording({ url: '/calls/m?tab=overview' });
    await screen.findByRole('tab', { name: 'Screen share (1)' });
    fireEvent.click(screen.getByRole('button', { name: /Slide at 00:30/ }));
    expect(selectedTab().textContent).toBe('Screen share (1)');
    expect(media().currentTime).toBe(30);
    expect(media().paused).toBe(true);
  });

  it('pauses when another tab is opened', async () => {
    await openRecording();
    fireEvent.click(screen.getByRole('button', { name: 'Play from 00:20.0' }));
    fireEvent.click(screen.getByRole('tab', { name: 'Overview' }));
    expect(media().paused).toBe(true);
  });

  it('plays and pauses on Space and moves 5 seconds on the arrows, but not while typing', async () => {
    await openRecording();
    fireEvent.keyDown(document.body, { key: ' ', code: 'Space' });
    expect(media().paused).toBe(false);
    fireEvent.keyDown(document.body, { key: ' ', code: 'Space' });
    expect(media().paused).toBe(true);
    fireEvent.keyDown(document.body, { key: 'ArrowRight' });
    fireEvent.keyDown(document.body, { key: 'ArrowRight' });
    expect(media().currentTime).toBe(10);
    fireEvent.keyDown(document.body, { key: 'ArrowLeft' });
    expect(media().currentTime).toBe(5);
    const search = screen.getByRole('searchbox', { name: 'Search the transcript' });
    fireEvent.keyDown(search, { key: ' ', code: 'Space' });
    fireEvent.keyDown(search, { key: 'ArrowLeft' });
    expect(media().paused).toBe(true);
    expect(media().currentTime).toBe(5);
  });

  it('leaves the keys alone on another tab', async () => {
    await openRecording({ url: '/calls/m?tab=details' });
    fireEvent.keyDown(document.body, { key: ' ', code: 'Space' });
    expect(media().paused).toBe(true);
  });

  it('marks the lines holding a search and steps through them', async () => {
    await openRecording();
    const search = screen.getByRole('searchbox', { name: 'Search the transcript' });
    fireEvent.change(search, { target: { value: 'budget' } });
    expect(screen.getByTestId('transcript-search-count').textContent).toBe('1 of 2');
    expect([...document.querySelectorAll('mark.transcript-search-match')].map((m) => m.textContent)).toEqual([
      'budget',
      'budget',
    ]);
    expect(rowOf('Thanks, Sarah.').className).not.toContain('transcript-row-match');
    expect(rowOf('Welcome to the budget review.').className).toContain('transcript-row-match-active');
    fireEvent.click(screen.getByRole('button', { name: 'Next match' }));
    expect(screen.getByTestId('transcript-search-count').textContent).toBe('2 of 2');
    expect(rowOf('The budget is four percent under plan.').className).toContain('transcript-row-match-active');
    fireEvent.click(screen.getByRole('button', { name: 'Next match' }));
    expect(screen.getByTestId('transcript-search-count').textContent).toBe('1 of 2');
    fireEvent.change(search, { target: { value: 'nothing like it' } });
    expect(screen.getByTestId('transcript-search-count').textContent).toBe('No matches');
  });

  it('shows only the speakers chosen', async () => {
    await openRecording();
    const filter = screen.getByRole('group', { name: 'Show speakers' });
    fireEvent.click(within(filter).getByRole('button', { name: 'Bob Chen' }));
    expect(screen.queryByText('Welcome to the budget review.')).toBeNull();
    expect(screen.getByText('Thanks, Sarah.')).toBeTruthy();
    fireEvent.click(within(filter).getByRole('button', { name: 'All speakers' }));
    expect(screen.getByText('Welcome to the budget review.')).toBeTruthy();
  });
});
