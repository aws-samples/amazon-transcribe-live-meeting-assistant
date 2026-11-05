/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The meeting page as a whole: the header above the tabs, which tab is shown
 * (and that the choice lives in the URL), the Screen share tab appearing once
 * the meeting has key frames, and the Summary tab showing the summary a section
 * at a time, the slides as thumbnails and the chapters once.
 *
 * Everything that talks to AWS is replaced: the GraphQL client answers the
 * handful of queries the page makes, and the contexts supply a signed-in user.
 */
import React from 'react';
import { describe, it, expect, vi, afterEach, beforeAll, beforeEach } from 'vitest';
import { act, render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { MemoryRouter, useLocation } from 'react-router-dom';

const responses = vi.hoisted(() => ({ frames: [], cost: null, vps: [], vncToggle: null }));
// The calls context, which a test can give the writers the page offers controls for.
const callsContext = vi.hoisted(() => ({ value: { calls: [] } }));
// What useMeetingConsent answers; nothing read yet unless a test says otherwise.
const consentState = vi.hoisted(() => ({ value: { consent: null } }));
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
vi.mock('../../contexts/calls', () => ({ default: () => callsContext.value }));
vi.mock('../../hooks/use-user-groups', () => ({ default: () => ({ isAdmin: false, isLoading: false }) }));
vi.mock('../../hooks/use-meeting-consent', () => ({ default: () => consentState.value }));
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
  callsContext.value = { calls: [] };
  consentState.value = { consent: null };
  // Which summary sections a viewer opened is remembered between visits.
  window.localStorage.removeItem('lma-summary-sections');
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
    fireEvent.click(screen.getByRole('tab', { name: 'Summary' }));
    expect(selectedTab().textContent).toBe('Summary');
    expect(screen.getByTestId('location').textContent).toBe('/calls/m?tab=summary');
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

describe('the Summary tab', () => {
  const openSection = (title) => fireEvent.click(screen.getByRole('button', { name: title, expanded: false }));

  it('shows the SUMMARY section open and the others closed, with links to each', () => {
    renderPage({ url: '/calls/m?tab=summary' });
    expect(screen.getByText('Sarah walked through the plan.')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'SUMMARY', expanded: true })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'SCREEN SHARE SUMMARY', expanded: false })).toBeTruthy();
    expect(screen.queryByText('Sarah covered Q3.')).toBeNull();
    const toc = screen.getByRole('navigation', { name: 'Summary sections' });
    fireEvent.click(within(toc).getByRole('button', { name: 'SCREEN SHARE SUMMARY' }));
    expect(screen.getByText('Sarah covered Q3.')).toBeTruthy();
  });

  it('opens and closes every section at once, and remembers the choice', () => {
    const { unmount } = renderPage({ url: '/calls/m?tab=summary' });
    fireEvent.click(screen.getByRole('button', { name: 'Expand all' }));
    expect(screen.getByText('Sarah covered Q3.')).toBeTruthy();
    unmount();
    renderPage({ url: '/calls/m?tab=summary' });
    expect(screen.getByText('Sarah covered Q3.')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Collapse all' }));
    expect(screen.queryByText('Sarah walked through the plan.')).toBeNull();
  });

  it.each(['SCREEN SHARE SUMMARY', 'SLIDES PRESENTED'])(
    'shows the frames its %s section names as thumbnails once opened, and opens one on Screen share',
    async (title) => {
      responses.frames = [frame(30000)];
      renderPage({ item: { ...ITEM, callSummaryText: summaryWith(title) }, url: '/calls/m?tab=summary' });
      await screen.findByRole('tab', { name: 'Screen share (1)' });
      const summary = screen.getByText('Sarah walked through the plan.').closest('[role="tabpanel"]');
      // Closed, the section reads no frame and shows none.
      expect(within(summary).queryAllByRole('img')).toHaveLength(0);
      openSection(title);
      const thumbnail = await within(summary).findByRole('img', { name: 'Slide at 00:30' });
      expect(thumbnail.getAttribute('src')).toBe('https://signed.example/30000_thumb.jpg');
      fireEvent.click(thumbnail);
      expect(selectedTab().textContent).toBe('Screen share (1)');
      expect(await screen.findByAltText('Shared screen at 0:30, full size')).toBeTruthy();
    },
  );

  it('lists the chapters once, as the list that moves the transcript', () => {
    renderPage({ url: '/calls/m?tab=summary' });
    expect(screen.getAllByText('Budget review')).toHaveLength(1);
    expect(screen.queryByText('CHAPTERS')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Budget review' }));
    expect(selectedTab().textContent).toBe('Transcript');
  });

  it('opens for a link saved when the tab was called Overview', () => {
    renderPage({ url: '/calls/m?tab=overview' });
    expect(selectedTab().textContent).toBe('Summary');
  });

  it('has no download of its own: the downloads are in the header', () => {
    renderPage({ url: '/calls/m?tab=summary' });
    expect(screen.queryByRole('button', { name: 'Download the summary' })).toBeNull();
    expect(screen.getByRole('button', { name: 'Download' })).toBeTruthy();
  });
});

describe('a message typed in the meeting chat', () => {
  const chat = (id, transcript, startTime, speaker = 'Bo Chen') => ({
    segmentId: id,
    createdAt: `2026-11-03T14:05:${String(startTime).padStart(2, '0')}.000Z`,
    channel: 'MEETING_CHAT',
    speaker,
    transcript,
    startTime,
    endTime: startTime,
    isPartial: false,
  });
  const spoken = (id, channel, speaker, transcript, startTime, endTime) => ({
    segmentId: id,
    createdAt: `2026-11-03T14:05:${String(startTime).padStart(2, '0')}.000Z`,
    channel,
    speaker,
    transcript,
    startTime,
    endTime,
    isPartial: false,
  });
  const WITH_CHAT = {
    [ITEM.callId]: {
      REMOTE: {
        segments: [
          spoken('s1', 'REMOTE', 'Sarah Lee', 'Welcome to the budget review.', 0, 8),
          spoken('s3', 'REMOTE', 'Sarah Lee', 'Any questions?', 30, 34),
        ],
      },
      LOCAL: { segments: [spoken('s2', 'LOCAL', 'Bob Chen', 'Thanks, Sarah.', 20, 24)] },
      MEETING_CHAT: {
        segments: [
          chat('chat-zoom-1', '**not bold** [the doc](https://example.com/doc)', 2),
          chat('chat-zoom-2', '+1', 3),
          chat('chat-zoom-3', 'Budget sheet: https://example.com/budget.', 26, 'Sarah Lee'),
        ],
      },
    },
  };
  const rows = () => [...document.querySelectorAll('[data-row-id]')];
  const rowOf = (text) => rows().find((row) => row.textContent.includes(text));
  const labels = () => [...document.querySelectorAll('.chat-segment-label')].map((l) => l.textContent);

  it('is shown under its sender as chat, as typed, one row per message', () => {
    renderPage({ transcript: WITH_CHAT });
    expect(labels()).toEqual(['Bo Chen · chat', 'Bo Chen · chat', 'Sarah Lee · chat']);
    // Not rendered as markdown: no emphasis and no link; the address is code to copy.
    const first = rowOf('**not bold**');
    expect(first.querySelector('.chat-segment-text').textContent).toBe(
      '**not bold** [the doc](https://example.com/doc)',
    );
    expect(document.querySelector('a[href]')).toBeNull();
    expect([...first.querySelectorAll('code')].map((c) => c.textContent)).toEqual(['https://example.com/doc']);
    expect(within(first).getByRole('button', { name: 'Copy https://example.com/doc' })).toBeTruthy();
    expect(screen.getByText('+1')).toBeTruthy();
  });

  it('is shown at its time among the spoken lines', () => {
    renderPage({ transcript: WITH_CHAT });
    expect(rows().map((row) => row.querySelector('.chat-segment-text, p')?.textContent)).toEqual([
      '**not bold** [the doc](https://example.com/doc)',
      '+1',
      'Welcome to the budget review.',
      'Thanks, Sarah.',
      'Budget sheet: https://example.com/budget.',
      'Any questions?',
    ]);
    expect(within(rowOf('+1')).getByText('00:03.0')).toBeTruthy();
  });

  it('offers no chat sender as a speaker, and shows or hides the chat with its own chip', () => {
    renderPage({ transcript: WITH_CHAT });
    const filter = screen.getByRole('group', { name: 'Show speakers' });
    expect(within(filter).queryByRole('button', { name: 'Bo Chen' })).toBeNull();
    expect(
      within(filter)
        .getAllByRole('button')
        .map((b) => b.textContent),
    ).toEqual(['All', 'Sarah Lee', 'Bob Chen', 'Chat']);
    const chip = within(filter).getByRole('button', { name: 'Chat messages' });
    expect(chip.getAttribute('aria-pressed')).toBe('true');
    fireEvent.click(chip);
    expect(document.querySelectorAll('.chat-segment')).toHaveLength(0);
    expect(screen.getByText('Thanks, Sarah.')).toBeTruthy();
    fireEvent.click(chip);
    expect(document.querySelectorAll('.chat-segment')).toHaveLength(3);
  });

  it("keeps the chat beside a chosen speaker's lines, not the speaker's chat alone", () => {
    renderPage({ transcript: WITH_CHAT });
    const filter = screen.getByRole('group', { name: 'Show speakers' });
    fireEvent.click(within(filter).getByRole('button', { name: 'Bob Chen' }));
    expect(screen.queryByText('Welcome to the budget review.')).toBeNull();
    expect(screen.getByText('Thanks, Sarah.')).toBeTruthy();
    expect(labels()).toHaveLength(3);
    fireEvent.click(within(filter).getByRole('button', { name: 'Chat messages' }));
    expect(labels()).toHaveLength(0);
    expect(rows()).toHaveLength(1);
  });

  it('offers the Chat chip with a single speaker, and says why nothing is shown with only chat hidden', () => {
    renderPage({
      transcript: {
        [ITEM.callId]: {
          MEETING_CHAT: { segments: [chat('chat-1', 'hello', 2)] },
        },
      },
    });
    const filter = screen.getByRole('group', { name: 'Show speakers' });
    expect(within(filter).queryByRole('button', { name: 'All speakers' })).toBeNull();
    fireEvent.click(within(filter).getByRole('button', { name: 'Chat messages' }));
    expect(screen.getByText('No lines to show with the chat hidden.')).toBeTruthy();
  });

  it('is found by the search, with its words marked in the plain text', () => {
    renderPage({ transcript: WITH_CHAT });
    fireEvent.change(screen.getByRole('searchbox', { name: 'Search the transcript' }), {
      target: { value: 'budget' },
    });
    expect(screen.getByTestId('transcript-search-count').textContent).toBe('1 of 2');
    const row = rowOf('Budget sheet');
    expect(row.className).toContain('transcript-row-match');
    expect([...row.querySelectorAll('mark.transcript-search-match')].map((m) => m.textContent)).toEqual([
      'Budget',
      'budget',
    ]);
    expect(row.querySelector('code mark').textContent).toBe('budget');
    expect(row.querySelector('.chat-segment-text').textContent).toBe('Budget sheet: https://example.com/budget.');
  });

  it('keeps the sender the chat gave, whatever names the voices are given', () => {
    renderPage({
      transcript: WITH_CHAT,
      item: { ...ITEM, speakerIdentities: JSON.stringify({ identities: { 'MEETING_CHAT#Bo Chen': { name: 'X' } } }) },
    });
    expect(labels()[0]).toBe('Bo Chen · chat');
  });

  it('appears in order as it arrives while the meeting is live', () => {
    const live = { ...ITEM, recordingStatusLabel: IN_PROGRESS_STATUS, recordingStatusIcon: 'in-progress' };
    const at = (chatSegments, speech) => ({
      [ITEM.callId]: {
        REMOTE: { segments: speech },
        MEETING_CHAT: { segments: chatSegments },
      },
    });
    const first = spoken('s1', 'REMOTE', 'Sarah Lee', 'Welcome.', 0, 8);
    const second = spoken('s2', 'REMOTE', 'Sarah Lee', 'Next item.', 12, 15);
    const view = render(
      <MemoryRouter>
        <CallPanel
          item={live}
          callTranscriptPerCallId={at([], [first])}
          setToolsOpen={vi.fn()}
          getCallDetailsFromCallIds={vi.fn()}
        />
      </MemoryRouter>,
    );
    const rerender = (transcript) =>
      view.rerender(
        <MemoryRouter>
          <CallPanel
            item={live}
            callTranscriptPerCallId={transcript}
            setToolsOpen={vi.fn()}
            getCallDetailsFromCallIds={vi.fn()}
          />
        </MemoryRouter>,
      );
    rerender(at([chat('c1', 'first message', 9)], [first]));
    rerender(at([chat('c1', 'first message', 9)], [first, second]));
    rerender(at([chat('c1', 'first message', 9), chat('c2', 'second message', 16)], [first, second]));
    expect(rows().map((row) => row.querySelector('.chat-segment-text, p')?.textContent)).toEqual([
      'Welcome.',
      'first message',
      'Next item.',
      'second message',
    ]);
  });

  it('keeps every channel the meeting has, however many', () => {
    const many = {
      [ITEM.callId]: {
        CHAT_ASSISTANT: { segments: [] },
        AGENT_ASSISTANT: { segments: [] },
        MEETING_ASSISTANT: { segments: [] },
        REMOTE: { segments: [spoken('s1', 'REMOTE', 'Sarah Lee', 'Welcome.', 0, 8)] },
        LOCAL: { segments: [spoken('s2', 'LOCAL', 'Bob Chen', 'Thanks.', 10, 12)] },
        MEETING_CHAT: { segments: [chat('c1', 'hello', 9)] },
      },
    };
    renderPage({ transcript: many });
    expect(screen.getByText('Welcome.')).toBeTruthy();
    expect(screen.getByText('Thanks.')).toBeTruthy();
    expect(screen.getByText('hello')).toBeTruthy();
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

  it('passes over a chat message while following the recording, and plays from its time when clicked', async () => {
    const withChat = {
      [ITEM.callId]: {
        ...LINES[ITEM.callId],
        MEETING_CHAT: {
          segments: [{ ...line('c1', 'MEETING_CHAT', 'Alice', 'Slides: https://example.com/s', 21, 21) }],
        },
      },
    };
    await openRecording({ transcript: withChat });
    // The chat message sits inside "Thanks, Sarah." (20-24 s): that line is the one heard.
    fireEvent.click(screen.getByRole('button', { name: 'Play from 00:21.0' }));
    expect(media().currentTime).toBe(21);
    expect(media().paused).toBe(false);
    expect(rowOf('Slides:').getAttribute('aria-current')).toBeNull();
    expect(rowOf('Thanks, Sarah.').getAttribute('aria-current')).toBe('true');
    act(() => {
      media().currentTime = 26;
      fireEvent(media(), new Event('timeupdate'));
    });
    expect(rowOf('Slides:').getAttribute('aria-current')).toBeNull();
  });

  it('marks the chapters on the seek bar', async () => {
    await openRecording();
    expect(screen.getAllByTestId('meeting-player-marker').map((m) => m.getAttribute('aria-label'))).toEqual([
      'Chapter at 00:00: Welcome',
      'Chapter at 05:12: Budget review',
    ]);
  });

  it('opens the Transcript tab and plays from a chapter selected on the Summary tab', async () => {
    await openRecording({ url: '/calls/m?tab=summary' });
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
    await openRecording({ url: '/calls/m?tab=summary' });
    await screen.findByRole('tab', { name: 'Screen share (1)' });
    fireEvent.click(screen.getByRole('button', { name: 'SCREEN SHARE SUMMARY', expanded: false }));
    fireEvent.click(await screen.findByRole('img', { name: 'Slide at 00:30' }));
    expect(selectedTab().textContent).toBe('Screen share (1)');
    expect(media().currentTime).toBe(30);
    expect(media().paused).toBe(true);
  });

  it('pauses when another tab is opened', async () => {
    await openRecording();
    fireEvent.click(screen.getByRole('button', { name: 'Play from 00:20.0' }));
    fireEvent.click(screen.getByRole('tab', { name: 'Summary' }));
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

    fireEvent.click(screen.getByRole('tab', { name: 'Summary' }));
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
    fireEvent.click(screen.getByRole('tab', { name: 'Summary' }));
    expect(selectedTab().textContent).toBe('Summary');
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
    fireEvent.click(screen.getByRole('tab', { name: 'Summary' }));
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
    fireEvent.click(screen.getByRole('tab', { name: 'Summary' }));
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

describe('the Download menu', () => {
  const openMenu = () => fireEvent.click(screen.getByRole('button', { name: 'Download' }));
  const menuItems = () => screen.getAllByRole('menuitem').map((item) => item.textContent);

  it('offers the summary and the transcript in the same formats, and a copy of each as Markdown', () => {
    renderPage();
    openMenu();
    expect(menuItems()).toEqual([
      'Markdown (.md)',
      'Word (.docx)',
      'Plain text (.txt)',
      'Copy to clipboard (Markdown)',
      'Email summary (beta)',
      'Markdown (.md)',
      'Word (.docx)',
      'Plain text (.txt)',
      'Excel (.xlsx)',
      'Copy to clipboard (Markdown)',
    ]);
  });

  it('copies the summary as Markdown, and says so', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
    renderPage();
    openMenu();
    fireEvent.click(screen.getAllByRole('menuitem', { name: /Copy to clipboard/ })[0]);
    await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1));
    expect(writeText.mock.calls[0][0]).toMatch(/^# Weekly sync\n/);
    expect(writeText.mock.calls[0][0]).toContain('## SUMMARY\n\nSarah walked through the plan.');
    expect(await screen.findByText('Summary copied as Markdown')).toBeTruthy();
  });
});

describe('naming a speaker from the Transcript tab', () => {
  const line = (id, channel, speaker, transcript, startTime) => ({
    segmentId: id,
    createdAt: `2026-11-03T14:05:${String(startTime).padStart(2, '0')}.000Z`,
    channel,
    speaker,
    transcript,
    startTime,
    endTime: startTime + 3,
    isPartial: false,
  });
  const LINES = {
    [ITEM.callId]: {
      REMOTE: { segments: [line('s1', 'REMOTE', 'Sarah Lee', 'Welcome.', 0)] },
      LOCAL: { segments: [line('s2', 'LOCAL', 'Bob Chen', 'Thanks, Sarah.', 10)] },
    },
  };

  it("renames a voice from its chip's speaker modal, through the same write as the Speakers panel", async () => {
    const sendUpdateSpeakerIdentities = vi.fn().mockResolvedValue(true);
    callsContext.value = { calls: [], sendUpdateSpeakerIdentities };
    renderPage({ transcript: LINES });
    const filter = screen.getByRole('group', { name: 'Show speakers' });
    fireEvent.click(within(filter).getByRole('button', { name: 'Name or combine Bob Chen' }));
    const dialog = screen.getByRole('dialog', { name: 'Speaker: Bob Chen' });
    fireEvent.change(within(dialog).getByRole('textbox', { name: 'Name' }), { target: { value: 'Robert Chen' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(sendUpdateSpeakerIdentities).toHaveBeenCalledTimes(1));
    const [callId, serialized] = sendUpdateSpeakerIdentities.mock.calls[0];
    expect(callId).toBe(ITEM.callId);
    expect(JSON.parse(serialized).identities['LOCAL#Bob Chen']).toMatchObject({
      name: 'Robert Chen',
      source: 'manual',
    });
  });

  it('offers no speaker modal to a user who may not name speakers', () => {
    callsContext.value = { calls: [], sendUpdateSpeakerIdentities: vi.fn() };
    renderPage({ transcript: LINES, item: { ...ITEM, owner: 'someone-else' } });
    expect(screen.queryByRole('button', { name: /^Name or combine/ })).toBeNull();
  });
});

describe('the consent that applied to the meeting', () => {
  const OPT_IN = {
    consent: { Mode: 'opt_in', Rule: 'each_participant', Status: 'granted', CaptureAllowed: true },
    captureAllowed: true,
    settings: { Mode: 'opt_in' },
  };
  const indicator = () => screen.queryByTestId('meeting-consent-indicator');
  const consentRecordHeading = () => screen.queryByRole('heading', { name: /^Consent/ });

  it('is not named in the header before the record has been read', () => {
    renderPage();
    expect(indicator()).toBeNull();
  });

  it('is named in the header, which opens the consent record on the Details tab', () => {
    consentState.value = OPT_IN;
    renderPage();
    expect(indicator().textContent).toBe('Consent Opt-in · granted');
    fireEvent.click(within(indicator()).getByRole('button'));
    expect(selectedTab().textContent).toBe('Details');
    expect(document.getElementById('meeting-consent-record')).toBeTruthy();
    expect(consentRecordHeading()).toBeTruthy();
  });

  it('says when consent enforcement was off, and shows no consent record', () => {
    consentState.value = { consent: null, captureAllowed: true, settings: { Mode: 'off' } };
    renderPage({ url: '/calls/m?tab=details' });
    expect(indicator().textContent).toBe('Consent enforcement off');
    expect(document.getElementById('meeting-consent-record')).toBeNull();
    expect(screen.getByText('Capture and processing')).toBeTruthy();
  });

  it('says above the tabs that transcription is paused while a live meeting waits for consent', () => {
    consentState.value = {
      consent: { Mode: 'opt_in', Status: 'pending', CaptureAllowed: false },
      captureAllowed: false,
      settings: { Mode: 'opt_in' },
    };
    const live = { ...ITEM, recordingStatusLabel: IN_PROGRESS_STATUS, recordingStatusIcon: 'in-progress' };
    const { unmount } = renderPage({ item: live });
    // The meeting started days ago, so the grace period is long past.
    expect(screen.getByText('Transcription is paused until consent is recorded')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Show the consent record' }));
    expect(selectedTab().textContent).toBe('Details');
    unmount();
    renderPage();
    expect(screen.queryByText('Transcription is paused until consent is recorded')).toBeNull();
  });
});
