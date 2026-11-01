/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The page that hosts the meeting assistant chat frame answers each of its
 * requests -- an answer, the answer's streamed tokens, the buttons, the user
 * context -- through the page's own GraphQL client, replies to the frame that
 * asked, and ignores messages from any other origin.
 */
import { renderHook, waitFor } from '@testing-library/react';
import { afterEach, describe, it, expect, vi } from 'vitest';

vi.mock('../contexts/app', () => ({
  default: () => ({ user: { username: 'owner@example.com', attributes: { email: 'owner@example.com' } } }),
}));
vi.mock('./use-user-groups', () => ({
  default: () => ({ userGroups: ['Admin'], isAdmin: true, isLoading: false }),
}));

// eslint-disable-next-line import/first
import useStrandsChatBridge, {
  ON_ADD_CHAT_TOKEN,
  SEND_CHAT_MESSAGE,
  mergeChatButtons,
} from './use-strands-chat-bridge';

const ask = (source, data, origin = window.location.origin) =>
  window.dispatchEvent(new MessageEvent('message', { origin, source, data }));

const fakeClient = (graphql) => ({ graphql: vi.fn(graphql) });

describe('useStrandsChatBridge', () => {
  let hook;
  afterEach(() => hook?.unmount());

  it('answers a chat request with sendChatMessage for the meeting the frame names', async () => {
    const result = { data: { sendChatMessage: { MessageId: 'm-9', Status: 'PROCESSING', CallId: 'c 1+x' } } };
    const client = fakeClient(async () => result);
    const source = { postMessage: vi.fn() };
    hook = renderHook(() => useStrandsChatBridge('c 1+x', { client }));
    ask(source, {
      type: 'STRANDS_CHAT_REQUEST',
      callId: 'c 1+x',
      message: 'What was decided?',
      messageId: 'msg-1',
      conversationHistory: [{ role: 'user', content: 'hi' }],
    });
    await waitFor(() => expect(source.postMessage).toHaveBeenCalled());
    expect(client.graphql).toHaveBeenCalledWith({
      query: SEND_CHAT_MESSAGE,
      variables: {
        input: {
          CallId: 'c 1+x',
          Message: 'What was decided?',
          ConversationHistory: [{ role: 'user', content: 'hi' }],
        },
      },
    });
    expect(source.postMessage).toHaveBeenCalledWith(
      { type: 'STRANDS_CHAT_RESPONSE', messageId: 'msg-1', success: true, result },
      window.location.origin,
    );
  });

  it('reports a failed chat request to the frame instead of leaving it waiting', async () => {
    const client = fakeClient(async () => {
      throw new Error('Unauthorized');
    });
    const source = { postMessage: vi.fn() };
    hook = renderHook(() => useStrandsChatBridge('c-1', { client }));
    ask(source, { type: 'STRANDS_CHAT_REQUEST', callId: 'c-1', message: 'hi', messageId: 'msg-2' });
    await waitFor(() => expect(source.postMessage).toHaveBeenCalled());
    expect(source.postMessage.mock.calls[0][0]).toEqual({
      type: 'STRANDS_CHAT_RESPONSE',
      messageId: 'msg-2',
      success: false,
      error: 'Unauthorized',
    });
  });

  it('streams the answer tokens to the frame, and stops when the page goes away', async () => {
    let observer;
    const unsubscribe = vi.fn();
    const client = fakeClient(() => ({
      subscribe: (o) => {
        observer = o;
        return { unsubscribe };
      },
    }));
    const source = { postMessage: vi.fn() };
    hook = renderHook(() => useStrandsChatBridge('c-1', { client }));
    ask(source, { type: 'STRANDS_SETUP_TOKEN_SUBSCRIPTION', callId: 'c-1', messageId: 'msg-1' });
    await waitFor(() => expect(observer).toBeDefined());
    expect(client.graphql).toHaveBeenCalledWith({
      query: ON_ADD_CHAT_TOKEN,
      variables: { callId: 'c-1', messageId: 'msg-1' },
    });
    const token = { CallId: 'c-1', MessageId: 'msg-1', Token: 'Hello', IsComplete: false, Sequence: 1 };
    observer.next({ data: { onAddChatToken: token } });
    expect(source.postMessage).toHaveBeenCalledWith({ type: 'STRANDS_TOKEN_MESSAGE', token }, window.location.origin);
    hook.unmount();
    hook = null;
    expect(unsubscribe).toHaveBeenCalled();
  });

  it("closes an answer's token stream once its final token has been passed on", async () => {
    let observer;
    const unsubscribe = vi.fn();
    const client = fakeClient(() => ({
      subscribe: (o) => {
        observer = o;
        return { unsubscribe };
      },
    }));
    const source = { postMessage: vi.fn() };
    hook = renderHook(() => useStrandsChatBridge('c-1', { client }));
    ask(source, { type: 'STRANDS_SETUP_TOKEN_SUBSCRIPTION', callId: 'c-1', messageId: 'msg-1' });
    await waitFor(() => expect(observer).toBeDefined());
    observer.next({ data: { onAddChatToken: { MessageId: 'msg-1', Token: 'Hi', IsComplete: false } } });
    expect(unsubscribe).not.toHaveBeenCalled();
    const last = { MessageId: 'msg-1', Token: '', IsComplete: true };
    observer.next({ data: { onAddChatToken: last } });
    expect(source.postMessage).toHaveBeenLastCalledWith(
      { type: 'STRANDS_TOKEN_MESSAGE', token: last },
      window.location.origin,
    );
    expect(unsubscribe).toHaveBeenCalledTimes(1);
    hook.unmount();
    hook = null;
    expect(unsubscribe).toHaveBeenCalledTimes(1);
  });

  it('answers the button request with the buttons in force', async () => {
    const client = fakeClient(async () => ({
      data: {
        default: { ChatButtonConfigId: JSON.stringify({ '1#SUMMARIZE': 'Summarize', ChatButtonConfigId: 'x' }) },
        custom: { ChatButtonConfigId: JSON.stringify({}) },
      },
    }));
    const source = { postMessage: vi.fn() };
    hook = renderHook(() => useStrandsChatBridge('c-1', { client }));
    ask(source, { type: 'STRANDS_BUTTON_CONFIG_REQUEST', requestId: 'r-1' });
    await waitFor(() => expect(source.postMessage).toHaveBeenCalled());
    expect(source.postMessage.mock.calls[0][0]).toEqual({
      type: 'STRANDS_BUTTON_CONFIG_RESPONSE',
      requestId: 'r-1',
      success: true,
      config: { '1#SUMMARIZE': 'Summarize' },
    });
  });

  it('answers a user context request with the signed-in user and groups', async () => {
    const source = { postMessage: vi.fn() };
    hook = renderHook(() => useStrandsChatBridge('c-1', { client: fakeClient(async () => ({})) }));
    ask(source, { type: 'STRANDS_REQUEST_USER_CONTEXT' });
    await waitFor(() => expect(source.postMessage).toHaveBeenCalled());
    expect(source.postMessage.mock.calls[0][0]).toEqual({
      type: 'STRANDS_USER_CONTEXT',
      userGroups: ['Admin'],
      isAdmin: true,
      email: 'owner@example.com',
    });
  });

  it('ignores a request from another origin', async () => {
    const client = fakeClient(async () => ({}));
    const source = { postMessage: vi.fn() };
    hook = renderHook(() => useStrandsChatBridge('c-1', { client }));
    ask(source, { type: 'STRANDS_CHAT_REQUEST', callId: 'c-1', message: 'hi', messageId: 'm' }, 'https://x.example');
    await new Promise((r) => {
      setTimeout(r, 10);
    });
    expect(client.graphql).not.toHaveBeenCalled();
    expect(source.postMessage).not.toHaveBeenCalled();
  });
});

describe('mergeChatButtons', () => {
  it('uses the custom buttons when there are any, and only N#LABEL keys', () => {
    const result = {
      data: {
        default: { ChatButtonConfigId: JSON.stringify({ '1#A': 'a' }) },
        custom: { ChatButtonConfigId: JSON.stringify({ '1#B': 'b', UpdatedAt: 'x' }) },
      },
    };
    expect(mergeChatButtons(result)).toEqual({ '1#B': 'b' });
    expect(mergeChatButtons({ data: { default: { ChatButtonConfigId: '{"1#A":"a"}' } } })).toEqual({ '1#A': 'a' });
  });
});
