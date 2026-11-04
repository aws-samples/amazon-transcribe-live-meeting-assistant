/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The embeddable transcript panel (show=transcript) shows a message typed in
 * the meeting chat as the meeting page does: under "<sender> · chat", at its
 * time among the spoken lines, as typed, with its addresses as code to copy
 * and never as links.
 */
import React from 'react';
import { render, screen } from '@testing-library/react';
import { describe, it, expect, vi } from 'vitest';

vi.mock('aws-amplify/api', () => ({ generateClient: () => ({ graphql: vi.fn() }) }));
vi.mock('../../contexts/settings', () => ({ default: () => ({ settings: {} }) }));
vi.mock('../../contexts/app', () => ({ default: () => ({ user: { username: 'alice' } }) }));
vi.mock('../../hooks/use-user-groups', () => ({
  default: () => ({ userGroups: [], isAdmin: false, isLoading: false }),
}));

// eslint-disable-next-line import/first
import { EmbedTranscriptPanel } from './EmbedCallDetails';

const CALL_ID = 'Weekly sync-2026-11-03-14-05-09-123';
const segment = (id, channel, speaker, transcript, startTime, endTime = startTime) => ({
  segmentId: id,
  createdAt: `2026-11-03T14:05:${String(startTime).padStart(2, '0')}.000Z`,
  channel,
  speaker,
  transcript,
  startTime,
  endTime,
  isPartial: false,
});

const TRANSCRIPT = {
  [CALL_ID]: {
    AGENT_VOICETONE: { segments: [] },
    CALLER_VOICETONE: { segments: [] },
    CHAT_ASSISTANT: { segments: [] },
    AGENT_ASSISTANT: { segments: [] },
    MEETING_ASSISTANT: { segments: [] },
    REMOTE: { segments: [segment('s1', 'REMOTE', 'Sarah Lee', 'Welcome.', 0, 8)] },
    LOCAL: { segments: [segment('s2', 'LOCAL', 'Bob Chen', 'Thanks.', 20, 24)] },
    MEETING_CHAT: {
      segments: [
        segment('c1', 'MEETING_CHAT', 'Alice', '**Deck**: https://example.com/deck)', 10),
        segment('c2', 'MEETING_CHAT', 'Alice', '+1', 11),
      ],
    },
  },
};

const renderPanel = () =>
  render(
    <EmbedTranscriptPanel
      item={{ callId: CALL_ID, recordingStatusLabel: 'Ended' }}
      callTranscriptPerCallId={TRANSCRIPT}
    />,
  );

describe('EmbedTranscriptPanel', () => {
  it('shows each chat message as its own row, as on the meeting page, among the spoken lines', () => {
    const { container } = renderPanel();
    expect([...container.querySelectorAll('.chat-segment-label')].map((l) => l.textContent)).toEqual([
      'Alice · chat',
      'Alice · chat',
    ]);
    const lines = [...container.querySelectorAll('.transcript-segment')].map(
      (row) => row.querySelector('.chat-segment-text, p')?.textContent,
    );
    expect(lines).toEqual(['Welcome.', '**Deck**: https://example.com/deck)', '+1', 'Thanks.']);
    expect(screen.getByText('00:10.0')).toBeTruthy();
  });

  it('shows the message as typed, its address as code to copy and never a link', () => {
    const { container } = renderPanel();
    const message = container.querySelector('.chat-segment');
    expect(message.querySelector('strong + strong, em, a')).toBeNull();
    expect(container.querySelector('a[href]')).toBeNull();
    expect([...message.querySelectorAll('code')].map((c) => c.textContent)).toEqual(['https://example.com/deck']);
    expect(screen.getByRole('button', { name: 'Copy https://example.com/deck' })).toBeTruthy();
  });
});
