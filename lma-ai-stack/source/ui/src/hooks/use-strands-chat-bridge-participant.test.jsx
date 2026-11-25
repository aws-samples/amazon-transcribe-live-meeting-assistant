/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The chat frame on a Participant's shared meeting page: its questions go
 * through sendChatMessage as anyone's do, and its chat buttons, which only
 * Admins and Users may read, are answered with none, without a request.
 */
import { renderHook, waitFor } from '@testing-library/react';
import { afterEach, describe, it, expect, vi } from 'vitest';

vi.mock('../contexts/app', () => ({
  default: () => ({ user: { username: 'pat@example.com', attributes: { email: 'pat@example.com' } } }),
}));
vi.mock('./use-user-groups', () => ({
  default: () => ({ userGroups: ['Participant'], isAdmin: false, role: 'Participant', isLoading: false }),
}));

// eslint-disable-next-line import/first
import useStrandsChatBridge, { SEND_CHAT_MESSAGE } from './use-strands-chat-bridge';

const ask = (source, data) =>
  window.dispatchEvent(new MessageEvent('message', { origin: window.location.origin, source, data }));

describe('useStrandsChatBridge for a Participant', () => {
  let hook;
  afterEach(() => hook?.unmount());

  it('answers the chat buttons request with none, without asking the API', async () => {
    const client = { graphql: vi.fn() };
    const source = { postMessage: vi.fn() };
    hook = renderHook(() => useStrandsChatBridge('m1', { client }));
    ask(source, { type: 'STRANDS_BUTTON_CONFIG_REQUEST', requestId: 'r1' });
    await waitFor(() => expect(source.postMessage).toHaveBeenCalled());
    expect(source.postMessage.mock.calls[0][0]).toEqual({
      type: 'STRANDS_BUTTON_CONFIG_RESPONSE',
      requestId: 'r1',
      success: true,
      config: {},
    });
    expect(client.graphql).not.toHaveBeenCalled();
  });

  it('sends a question through sendChatMessage', async () => {
    const client = {
      graphql: vi.fn(async () => ({ data: { sendChatMessage: { MessageId: 'm-1', Status: 'PROCESSING' } } })),
    };
    const source = { postMessage: vi.fn() };
    hook = renderHook(() => useStrandsChatBridge('m1', { client }));
    ask(source, { type: 'STRANDS_CHAT_REQUEST', callId: 'm1', message: 'What was decided?', messageId: 'q1' });
    await waitFor(() => expect(client.graphql).toHaveBeenCalled());
    expect(client.graphql.mock.calls[0][0].query).toBe(SEND_CHAT_MESSAGE);
  });
});
