/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The `vp-details` embed renders the full meeting panel once its Virtual
 * Participant has a meeting: the header and the tabs, with the tab held in
 * the embed rather than in the embedding page's URL, and the recording's
 * player on the Transcript tab once the meeting has ended.
 */
import React from 'react';
import { describe, it, expect, vi, afterEach, beforeAll, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter, useLocation } from 'react-router-dom';

vi.mock('aws-amplify/api', () => {
  const answer = ({ query }) => {
    if (query.includes('getVirtualParticipant')) {
      return {
        data: {
          getVirtualParticipant: { id: 'vp-1', status: 'COMPLETED', CallId: 'Weekly sync-2026-11-03-14-05-09-123' },
        },
      };
    }
    if (query.includes('listVirtualParticipants')) return { data: { listVirtualParticipants: [] } };
    if (query.includes('getVideoKeyframes')) return { data: { getVideoKeyframes: { VideoKeyframes: [] } } };
    return { data: {} };
  };
  const graphql = (options) => {
    const result = Promise.resolve(answer(options));
    result.subscribe = () => ({ unsubscribe: () => undefined });
    return result;
  };
  return { generateClient: () => ({ graphql }) };
});

const item = vi.hoisted(() => ({
  callId: 'Weekly sync-2026-11-03-14-05-09-123',
  recordingStatusLabel: 'Done',
  recordingStatusIcon: 'success',
  initiationTimeStamp: '2026-11-03T14:05:09.123Z',
  updatedAt: '2026-11-03T14:30:00.000Z',
  owner: 'alice',
  videoRecordingUrl: 'https://bucket.s3.us-east-1.amazonaws.com/lma-video-recordings/m.mp4',
}));

vi.mock('../../hooks/use-calls-graphql-api', () => ({
  default: () => ({
    calls: [],
    callTranscriptPerCallId: {},
    getCallDetailsFromCallIds: vi.fn().mockResolvedValue([{}]),
    sendGetTranscriptSegmentsRequest: vi.fn().mockResolvedValue(undefined),
    setLiveTranscriptCallId: vi.fn(),
    setIsCallsListLoading: vi.fn(),
    setDateRange: vi.fn(),
  }),
}));
vi.mock('../common/map-call-attributes', () => ({ default: () => [item] }));
vi.mock('../../contexts/app', () => ({
  default: () => ({ currentCredentials: {}, user: { username: 'alice' }, setErrorMessage: vi.fn() }),
}));
vi.mock('../../contexts/settings', () => ({ default: () => ({ settings: {} }) }));
vi.mock('../../hooks/use-user-groups', () => ({ default: () => ({ isAdmin: false, isLoading: false }) }));
vi.mock('../../hooks/use-meeting-consent', () => ({ default: () => ({ consent: null }) }));
vi.mock('../../hooks/use-strands-chat-bridge', () => ({ default: () => undefined }));
vi.mock('../virtual-participant-layout/VNCViewer', () => ({ default: () => <div data-testid="vnc" /> }));
vi.mock('../mcp-servers', () => ({ default: () => null }));
vi.mock('../common/generate-s3-presigned-url', () => ({
  default: vi.fn().mockResolvedValue('https://signed.example/x'),
}));

// eslint-disable-next-line import/first
import EmbedVirtualParticipant from './EmbedVirtualParticipant';
// eslint-disable-next-line import/first
import { installMediaElementStub } from '../meeting-player/media-element-stub';

const LocationProbe = () => {
  const location = useLocation();
  return <div data-testid="location">{`${location.pathname}${location.search}`}</div>;
};

beforeAll(installMediaElementStub);
beforeEach(() => {
  Element.prototype.scrollTo = vi.fn();
  Element.prototype.scrollIntoView = vi.fn();
});

describe('the vp-details embed', () => {
  it("shows the meeting's tabs, keeps the tab to itself, and plays the recording on the Transcript tab", async () => {
    render(
      <MemoryRouter initialEntries={['/embed?component=vp-details&vpId=vp-1&show=transcript,chat']}>
        <EmbedVirtualParticipant
          params={{ vpId: 'vp-1', show: ['transcript', 'chat'], layout: 'vertical' }}
          sendToParent={vi.fn()}
        />
        <LocationProbe />
      </MemoryRouter>,
    );
    const details = await screen.findByRole('tab', { name: 'Details' });
    expect(screen.getByRole('tab', { name: 'Transcript' }).getAttribute('aria-selected')).toBe('true');
    await waitFor(() => expect(screen.getByTestId('meeting-player')).toBeTruthy());
    expect(screen.getByTestId('meeting-player').querySelector('video')).toBeTruthy();
    fireEvent.click(details);
    expect(details.getAttribute('aria-selected')).toBe('true');
    expect(screen.getByTestId('location').textContent).toBe(
      '/embed?component=vp-details&vpId=vp-1&show=transcript,chat',
    );
  });

  describe('with the meeting assistant enabled', () => {
    beforeEach(() => {
      vi.stubEnv('VITE_ENABLE_AGENT_ASSIST', 'true');
      window.localStorage.clear();
    });
    afterEach(() => vi.unstubAllEnvs());

    const renderEmbed = (show) =>
      render(
        <MemoryRouter initialEntries={[`/embed?component=vp-details&vpId=vp-1&show=${show.join(',')}`]}>
          <EmbedVirtualParticipant params={{ vpId: 'vp-1', show, layout: 'vertical' }} sendToParent={vi.fn()} />
        </MemoryRouter>,
      );

    it('opens the assistant panel when the embed asks for the chat', async () => {
      renderEmbed(['transcript', 'chat']);
      const button = await screen.findByRole('button', { name: /^Assistant/ });
      expect(button.getAttribute('aria-pressed')).toBe('true');
      expect(document.querySelector('iframe[src*="strands-chat.html"]')).not.toBeNull();
    });

    it('offers it behind the Assistant button when the embed does not', async () => {
      renderEmbed(['transcript']);
      const button = await screen.findByRole('button', { name: /^Assistant/ });
      expect(button.getAttribute('aria-pressed')).toBe('false');
      expect(document.querySelector('iframe[src*="strands-chat.html"]')).toBeNull();
    });
  });
});
