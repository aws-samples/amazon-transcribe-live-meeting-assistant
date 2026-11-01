/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The embeddable chat panel (component=chat, or show= without all three
 * panels) hosts the meeting assistant chat frame and answers its requests, as
 * the meeting page does: a chat message goes to sendChatMessage for the
 * panel's meeting, and the answer comes back to the frame.
 */
import React from 'react';
import { render, waitFor } from '@testing-library/react';
import { afterEach, describe, it, expect, vi } from 'vitest';

const { graphql } = vi.hoisted(() => ({ graphql: vi.fn() }));

vi.mock('aws-amplify/api', () => ({ generateClient: () => ({ graphql }) }));
vi.mock('../../contexts/app', () => ({
  default: () => ({ user: { username: 'friend@example.com' } }),
}));
vi.mock('../../hooks/use-user-groups', () => ({
  default: () => ({ userGroups: [], isAdmin: false, isLoading: false }),
}));

// eslint-disable-next-line import/first
import { EmbedChatPanel } from './EmbedCallDetails';

describe('EmbedChatPanel', () => {
  afterEach(() => {
    graphql.mockReset();
    vi.unstubAllEnvs();
  });

  it('answers the chat frame for the meeting it shows', async () => {
    vi.stubEnv('VITE_ENABLE_AGENT_ASSIST', 'true');
    const result = { data: { sendChatMessage: { MessageId: 'm-1', Status: 'PROCESSING', CallId: 'Team sync+1' } } };
    graphql.mockResolvedValue(result);
    const { container, unmount } = render(<EmbedChatPanel item={{ callId: 'Team sync+1' }} />);
    expect(container.querySelector('iframe').getAttribute('src')).toBe('/strands-chat.html?callId=Team%20sync%2B1');

    const source = { postMessage: vi.fn() };
    window.dispatchEvent(
      new MessageEvent('message', {
        origin: window.location.origin,
        source,
        data: { type: 'STRANDS_CHAT_REQUEST', callId: 'Team sync+1', message: 'Summarize', messageId: 'q-1' },
      }),
    );
    await waitFor(() => expect(source.postMessage).toHaveBeenCalled());
    expect(graphql.mock.calls[0][0].variables.input).toEqual({
      CallId: 'Team sync+1',
      Message: 'Summarize',
      ConversationHistory: [],
    });
    expect(source.postMessage.mock.calls[0][0]).toEqual({
      type: 'STRANDS_CHAT_RESPONSE',
      messageId: 'q-1',
      success: true,
      result,
    });
    unmount();
  });
});
