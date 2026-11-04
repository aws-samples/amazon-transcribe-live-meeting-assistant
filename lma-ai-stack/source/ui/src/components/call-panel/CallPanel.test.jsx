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
import { describe, it, expect, vi, afterEach, beforeAll, beforeEach } from 'vitest';
import { act, render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { MemoryRouter, useLocation } from 'react-router-dom';

const responses = vi.hoisted(() => ({ frames: [], cost: null, vps: [], vncToggle: null }));
// The options the page gives the chat bridge, so a test can finish an answer.
const bridge = vi.hoisted(() => ({ options: null }));

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
    result.subscribe = (observer) => {
      // The assistant opening and closing the live view.
      if (options.query.includes('OnVNCPreviewToggle')) responses.vncToggle = observer;
      return { unsubscribe: () => undefined };
    };
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
vi.mock('../../hooks/use-strands-chat-bridge', () => ({
  default: (callId, options) => {
    bridge.options = options;
  },
}));
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

const SCREEN_SHARE = '**[00:30] Budget**\n\n![Slide at 00:30](keyframe:0000030000-ab12cd)\n\nSarah covered Q3.';
// `title` is the screen share section's title: SLIDES PRESENTED in a summary
// written before it was renamed.
const summaryWith = (title) =>
  JSON.stringify({
    SUMMARY: 'Sarah walked through the plan.',
    [title]: SCREEN_SHARE,
    CHAPTERS: '- [00:00] Welcome\n- [05:12] Budget review',
  });
const SUMMARY = summaryWith('SCREEN SHARE SUMMARY');

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

const renderPage = ({
  item = ITEM,
  url = '/calls/m',
  tabInUrl = true,
  transcript = TRANSCRIPT,
  assistantDefaultOpen = undefined,
} = {}) =>
  render(
    <MemoryRouter initialEntries={[url]}>
      <CallPanel
        item={item}
        callTranscriptPerCallId={transcript}
        setToolsOpen={vi.fn()}
        getCallDetailsFromCallIds={vi.fn()}
        tabInUrl={tabInUrl}
        assistantDefaultOpen={assistantDefaultOpen}
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
  it.each(['SCREEN SHARE SUMMARY', 'SLIDES PRESENTED'])(
    'shows the frames its %s section names as links, not as pictures, and opens one on Screen share',
    async (title) => {
      responses.frames = [frame(30000)];
      renderPage({ item: { ...ITEM, callSummaryText: summaryWith(title) }, url: '/calls/m?tab=overview' });
      await screen.findByRole('tab', { name: 'Screen share (1)' });
      const overview = screen.getByText('Sarah walked through the plan.').closest('[role="tabpanel"]');
      expect(within(overview).queryAllByRole('img')).toHaveLength(0);
      fireEvent.click(within(overview).getByRole('button', { name: /Slide at 00:30/ }));
      expect(selectedTab().textContent).toBe('Screen share (1)');
      expect(await screen.findByAltText('Shared screen at 0:30, full size')).toBeTruthy();
    },
  );

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

  describe('a Virtual Participant meeting', () => {
    const vpJoined = () => {
      responses.vps = [{ id: 'vp-1', CallId: ITEM.callId, meetingPlatform: 'ZOOM', vncReady: false }];
    };

    it('plays the audio when its video has no key frames, says why, and keeps the video selectable', async () => {
      vpJoined();
      renderPage({ item: RECORDED, transcript: LINES });
      await waitFor(() => expect(media()?.tagName).toBe('AUDIO'));
      expect(screen.getByTestId('meeting-player-hint').textContent).toMatch(/no key frames/);
      fireEvent.click(screen.getByRole('tab', { name: 'Details' }));
      expect(screen.getByText(/plays the audio recording for this meeting/)).toBeTruthy();
      fireEvent.click(screen.getByRole('tab', { name: 'Transcript' }));
      fireEvent.click(screen.getByRole('button', { name: 'Video' }));
      await waitFor(() => expect(media()?.tagName).toBe('VIDEO'));
      expect(screen.queryByTestId('meeting-player-hint')).toBeNull();
    });

    it('plays the video when its key frames line it up', async () => {
      vpJoined();
      responses.frames = [keyframe(300, 330)];
      renderPage({ item: RECORDED, transcript: LINES });
      await screen.findByRole('tab', { name: 'Screen share (1)' });
      await waitFor(() => expect(media()?.tagName).toBe('VIDEO'));
      expect(screen.queryByTestId('meeting-player-hint')).toBeNull();
    });

    it('plays the video recording from the Details tab when asked', async () => {
      vpJoined();
      renderPage({ item: RECORDED, transcript: LINES, url: '/calls/m?tab=details' });
      await waitFor(() => expect(media()?.tagName).toBe('AUDIO'));
      fireEvent.click(screen.getByRole('button', { name: 'Play the video recording' }));
      expect(selectedTab().textContent).toBe('Transcript');
      await waitFor(() => expect(media()?.tagName).toBe('VIDEO'));
      await waitFor(() => expect(media().getAttribute('src')).toBe('https://signed.example/x'));
      loadMedia(media(), 1200);
      expect(media().paused).toBe(false);
    });
  });

  it('plays the video of a meeting without a Virtual Participant even with no key frames', async () => {
    await openRecording();
    await waitFor(() => expect(screen.getAllByRole('tab')).toHaveLength(3));
    expect(media().tagName).toBe('VIDEO');
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

describe('the meeting assistant panel', () => {
  const LIVE = { ...ITEM, recordingStatusLabel: IN_PROGRESS_STATUS, recordingStatusIcon: 'in-progress' };
  const assistantButton = () => screen.getByRole('button', { name: /^Assistant/ });
  const panel = () => document.getElementById('meeting-assistant-panel');
  const chatFrame = () => document.querySelector('iframe[src*="strands-chat.html"]');
  const fromFrame = (data, source = chatFrame().contentWindow) =>
    act(() => {
      window.dispatchEvent(new MessageEvent('message', { data, origin: window.location.origin, source }));
    });

  // The page's measured width: wide enough for the panel beside the tabs
  // unless a test narrows it.
  let pageWidth;
  // A user who has closed the panel before, for tests that start from closed.
  const chooseClosed = () => window.localStorage.setItem('lma-meeting-assistant-open:alice', 'false');

  beforeEach(() => {
    vi.stubEnv('VITE_ENABLE_AGENT_ASSIST', 'true');
    window.localStorage.clear();
    bridge.options = null;
    pageWidth = vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockReturnValue(1200);
  });
  afterEach(() => {
    vi.unstubAllEnvs();
    pageWidth.mockRestore();
  });

  it('has no button and no panel when the assistant is not enabled', () => {
    vi.stubEnv('VITE_ENABLE_AGENT_ASSIST', 'false');
    renderPage({ item: LIVE });
    expect(screen.queryByRole('button', { name: /^Assistant/ })).toBeNull();
    expect(panel()).toBeNull();
    expect(chatFrame()).toBeNull();
  });

  it('starts open beside the tabs when the user has made no choice, and closes to give the tabs the full width', () => {
    renderPage();
    const body = document.querySelector('.meeting-body');
    expect(assistantButton().getAttribute('aria-pressed')).toBe('true');
    expect(panel().hidden).toBe(false);
    expect(chatFrame()).not.toBeNull();
    expect(body.classList.contains('meeting-body-assistant-open')).toBe(true);
    expect(body.classList.contains('meeting-body-assistant-overlay')).toBe(false);
    // Opening by itself does not take the focus.
    expect(document.activeElement).toBe(document.body);

    fireEvent.click(assistantButton());
    expect(assistantButton().getAttribute('aria-pressed')).toBe('false');
    expect(panel().hidden).toBe(true);
    expect(body.classList.contains('meeting-body-assistant-open')).toBe(false);
  });

  it('starts closed where it would lie over the tabs, when the user has made no choice', () => {
    pageWidth.mockReturnValue(600);
    renderPage();
    expect(assistantButton().getAttribute('aria-pressed')).toBe('false');
    expect(chatFrame()).toBeNull();
    fireEvent.click(assistantButton());
    expect(panel().hidden).toBe(false);
    expect(document.querySelector('.meeting-body').classList.contains('meeting-body-assistant-overlay')).toBe(true);
  });

  it('starts closed for a user who closed it, even on a wide page', () => {
    chooseClosed();
    renderPage();
    expect(assistantButton().getAttribute('aria-pressed')).toBe('false');
    expect(chatFrame()).toBeNull();
    expect(document.querySelector('.meeting-body').classList.contains('meeting-body-assistant-open')).toBe(false);
  });

  it('remembers whether the user left it open', () => {
    chooseClosed();
    const first = renderPage();
    fireEvent.click(assistantButton());
    first.unmount();

    const second = renderPage();
    expect(assistantButton().getAttribute('aria-pressed')).toBe('true');
    fireEvent.click(assistantButton());
    second.unmount();

    renderPage();
    expect(assistantButton().getAttribute('aria-pressed')).toBe('false');
  });

  it('opens by itself the first time a live meeting is viewed, without taking the focus', () => {
    chooseClosed();
    pageWidth.mockReturnValue(600);
    const first = renderPage({ item: LIVE });
    expect(assistantButton().getAttribute('aria-pressed')).toBe('true');
    expect(panel().hidden).toBe(false);
    expect(document.activeElement).toBe(document.body);
    fireEvent.click(assistantButton());
    first.unmount();

    // Seen once; from then on it is as the user left it.
    renderPage({ item: LIVE });
    expect(assistantButton().getAttribute('aria-pressed')).toBe('false');
  });

  it('keeps the same chat frame across tab changes and closing and reopening', () => {
    chooseClosed();
    renderPage();
    fireEvent.click(assistantButton());
    const chat = chatFrame();

    fireEvent.click(screen.getByRole('tab', { name: 'Overview' }));
    expect(chatFrame()).toBe(chat);
    expect(panel().hidden).toBe(false);

    fireEvent.click(assistantButton());
    expect(panel().hidden).toBe(true);
    expect(chatFrame()).toBe(chat);

    fireEvent.click(screen.getByRole('tab', { name: 'Details' }));
    fireEvent.click(assistantButton());
    expect(panel().hidden).toBe(false);
    expect(chatFrame()).toBe(chat);
  });

  it('puts the focus in the composer when opened', () => {
    chooseClosed();
    renderPage();
    fireEvent.click(assistantButton());
    const chat = chatFrame();
    const post = vi.spyOn(chat.contentWindow, 'postMessage');
    fireEvent.click(assistantButton());
    fireEvent.click(assistantButton());
    expect(document.activeElement).toBe(chat);
    expect(post).toHaveBeenCalledWith({ type: 'STRANDS_FOCUS_INPUT' }, window.location.origin);
  });

  it('closes on Escape from within the panel, returning the focus to the Assistant button', () => {
    chooseClosed();
    renderPage();
    fireEvent.click(assistantButton());
    const close = within(panel()).getByRole('button', { name: 'Close the assistant' });
    close.focus();
    fireEvent.keyDown(close, { key: 'Escape' });
    expect(panel().hidden).toBe(true);
    expect(assistantButton().getAttribute('aria-pressed')).toBe('false');
    expect(document.activeElement).toBe(assistantButton());
  });

  it('closes on Escape pressed in the chat frame, and ignores the message from any other window', () => {
    chooseClosed();
    renderPage();
    fireEvent.click(assistantButton());
    fromFrame({ type: 'STRANDS_ESCAPE' }, window);
    expect(panel().hidden).toBe(false);
    fromFrame({ type: 'STRANDS_ESCAPE' });
    expect(panel().hidden).toBe(true);
  });

  it('leaves Escape alone when the focus is elsewhere on the page', () => {
    chooseClosed();
    renderPage();
    fireEvent.click(assistantButton());
    fireEvent.keyDown(screen.getByRole('tab', { name: 'Transcript' }), { key: 'Escape' });
    expect(panel().hidden).toBe(false);
  });

  it("opens the Transcript tab at a frame the assistant showed, from the frame's Show in transcript", () => {
    chooseClosed();
    renderPage();
    fireEvent.click(assistantButton());
    fireEvent.click(screen.getByRole('tab', { name: 'Overview' }));
    expect(selectedTab().textContent).toBe('Overview');
    fromFrame({ type: 'STRANDS_SHOW_IN_TRANSCRIPT', frameId: '0000030000-ab12cd' });
    expect(selectedTab().textContent).toBe('Transcript');
    // Beside the tabs, the panel stays open.
    expect(panel().hidden).toBe(false);
  });

  it('lies over the tabs on a narrow page, and makes way when it shows a moment in the transcript', () => {
    pageWidth.mockReturnValue(600);
    renderPage();
    fireEvent.click(assistantButton());
    const body = document.querySelector('.meeting-body');
    expect(body.classList.contains('meeting-body-assistant-overlay')).toBe(true);
    fireEvent.click(screen.getByRole('tab', { name: 'Overview' }));
    fromFrame({ type: 'STRANDS_SHOW_IN_TRANSCRIPT', frameId: '0000030000-ab12cd' });
    expect(selectedTab().textContent).toBe('Transcript');
    expect(panel().hidden).toBe(true);
    // Closed for the viewer, not by them: the choice to have it open is kept.
    expect(window.localStorage.getItem('lma-meeting-assistant-open:alice')).toBe('true');
  });

  it('is sized again to the window when the header above it changes height', () => {
    const observers = [];
    vi.stubGlobal(
      'ResizeObserver',
      class {
        constructor(callback) {
          this.callback = callback;
          this.targets = [];
          observers.push(this);
        }

        observe(target) {
          this.targets.push(target);
        }

        disconnect() {
          this.targets = [];
        }
      },
    );
    let top = 200;
    const rect = vi
      .spyOn(HTMLElement.prototype, 'getBoundingClientRect')
      .mockImplementation(() => ({ top, left: 0, right: 0, bottom: 0, width: 0, height: 0 }));
    try {
      renderPage();
      const body = document.querySelector('.meeting-body');
      const height = () => body.style.getPropertyValue('--meeting-assistant-height');
      expect(height()).toBe(`${window.innerHeight - 200 - 12}px`);
      const observer = observers.find((o) => o.targets.includes(body));
      // The same observer watches the header.
      const title = screen.getByTestId('meeting-title');
      expect(observer.targets.some((target) => target !== body && target.contains(title))).toBe(true);
      // Speaker names arrive and the header grows: the panel starts lower.
      top = 260;
      act(() => observer.callback());
      expect(height()).toBe(`${window.innerHeight - 260 - 12}px`);
    } finally {
      rect.mockRestore();
      vi.unstubAllGlobals();
    }
  });

  it('marks the Assistant button when an answer finishes while the panel is closed', () => {
    chooseClosed();
    renderPage();
    act(() => bridge.options.onAnswerComplete());
    expect(assistantButton().textContent).toContain('new answer');
    fireEvent.click(assistantButton());
    expect(assistantButton().textContent).not.toContain('new answer');
    act(() => bridge.options.onAnswerComplete());
    expect(assistantButton().textContent).not.toContain('new answer');
  });

  it('shows the live view at the top of the Transcript tab when the assistant opens it', async () => {
    responses.vps = [{ id: 'vp-1', CallId: ITEM.callId, meetingPlatform: 'ZOOM', vncReady: true }];
    renderPage({ item: LIVE });
    expect((await screen.findAllByText('Zoom')).length).toBeGreaterThan(0);
    fireEvent.click(screen.getByRole('tab', { name: 'Overview' }));
    expect(screen.queryByTestId('vnc')).toBeNull();
    act(() => responses.vncToggle.next({ data: { onVNCPreviewToggle: { Success: true, Action: 'open' } } }));
    expect(selectedTab().textContent).toBe('Transcript');
    expect(screen.getByTestId('vnc')).not.toBeNull();
    act(() => responses.vncToggle.next({ data: { onVNCPreviewToggle: { Success: true, Action: 'close' } } }));
    expect(screen.queryByTestId('vnc')).toBeNull();
  });

  it("opens as an embed asks, without changing the user's remembered choice", () => {
    chooseClosed();
    pageWidth.mockReturnValue(600);
    const embed = renderPage({ tabInUrl: false, assistantDefaultOpen: true });
    expect(panel().hidden).toBe(false);
    fireEvent.click(assistantButton());
    fireEvent.click(assistantButton());
    embed.unmount();
    renderPage();
    expect(assistantButton().getAttribute('aria-pressed')).toBe('false');
    expect(window.localStorage.getItem('lma-meeting-assistant-open:alice')).toBe('false');
  });
});
