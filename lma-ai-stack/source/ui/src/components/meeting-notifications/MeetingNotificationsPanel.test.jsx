/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * A meeting's notifications on its page: loaded, then live, in time order;
 * dismissed and muted per user; expired ones hidden; buttons only for actions
 * this page knows; Markdown without raw HTML or pictures.
 */
import React from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, renderHook, screen, waitFor, within } from '@testing-library/react';

import MeetingNotificationsPanel from './MeetingNotificationsPanel';
import MeetingNotificationsInline from './MeetingNotificationsInline';
import useMeetingNotifications, { mergeNotification } from '../../hooks/use-meeting-notifications';
import { MUTATION_ACTIONS, notificationActions, resolveAction } from './notification-actions';
import { meetingCallIdOf } from './meeting-route';
import fakeClient, { notification } from './__mocks__/notification-client';

const answersWith = (items, state = {}, muted = []) => ({
  listMeetingNotifications: { Items: items, NextToken: null },
  getMyMeetingNotificationState: { CallId: 'm1', Dismissed: [], MutedCategories: [], ...state },
  getMyNotificationPreferences: { MutedCategories: muted },
  updateMyMeetingNotificationState: (variables) => ({
    CallId: 'm1',
    Dismissed: variables.input.Dismiss || [],
    MutedCategories: variables.input.Mute || [],
  }),
});

// eslint-disable-next-line react/prop-types
const PanelWithHook = ({ client, now }) => {
  const notifications = useMeetingNotifications('m1', { client, now });
  return <MeetingNotificationsPanel notifications={notifications} />;
};

const titles = () =>
  screen.queryAllByTestId('meeting-notification').map((li) => within(li).getByRole('heading').textContent);

describe('useMeetingNotifications', () => {
  it('loads the earlier notifications and subscribes to this meeting with the access fields', async () => {
    const { client, state } = fakeClient(answersWith([notification('a')]));
    const { result } = renderHook(() => useMeetingNotifications('m1', { client }));
    await waitFor(() => expect(result.current.items).toHaveLength(1));
    const subscription = state.requests.find((request) => request.query.includes('subscription'));
    expect(subscription.variables).toEqual({ callId: 'm1' });
    expect(subscription.query).toMatch(/\bOwner\b/);
    expect(subscription.query).toMatch(/\bSharedWith\b/);
    const list = state.requests.find((request) => request.query.includes('listMeetingNotifications('));
    expect(list.variables.callId).toBe('m1');
    // Newest first, so a meeting with more than the page loads keeps its latest.
    expect(list.variables.newestFirst).toBe(true);
    expect(list.query).toContain('NewestFirst: $newestFirst');
  });

  it('adds live notifications in time order and replaces an update in place', async () => {
    const { client, send } = fakeClient(answersWith([notification('b', { CreatedAt: '2026-11-19T14:05:00.000Z' })]));
    const { result } = renderHook(() => useMeetingNotifications('m1', { client }));
    await waitFor(() => expect(result.current.items).toHaveLength(1));
    act(() => send(notification('a', { CreatedAt: '2026-11-19T14:01:00.000Z' })));
    act(() => send(notification('b', { CreatedAt: '2026-11-19T14:05:00.000Z', Title: 'Updated', Revision: 2 })));
    expect(result.current.items.map((item) => item.NotificationId)).toEqual(['a', 'b']);
    expect(result.current.items[1].Title).toBe('Updated');
    expect(result.current.liveArrivals).toBe(2);
  });

  it('ignores a message for another meeting', async () => {
    const { client, send } = fakeClient(answersWith([]));
    const { result } = renderHook(() => useMeetingNotifications('m1', { client }));
    act(() => send(notification('x', { CallId: 'm2' })));
    expect(result.current.items).toHaveLength(0);
  });

  it('keeps a newer revision over an older one that arrives late', () => {
    const newer = notification('a', { Revision: 3, Title: 'newer' });
    const merged = mergeNotification({ a: newer }, notification('a', { Revision: 2, Title: 'older' }));
    expect(merged.a.Title).toBe('newer');
  });

  it('hides what this user dismissed or muted, and what has expired', async () => {
    const items = [
      notification('dismissed'),
      notification('muted-here', { Category: 'status' }),
      notification('muted-everywhere', { Category: 'tips' }),
      notification('expired', { ExpiresAt: '2026-11-19T14:00:00.000Z' }),
      notification('shown'),
    ];
    const { client } = fakeClient(
      answersWith(items, { Dismissed: ['dismissed'], MutedCategories: ['status'] }, ['tips']),
    );
    const now = Date.parse('2026-11-19T15:00:00.000Z');
    const { result } = renderHook(() => useMeetingNotifications('m1', { client, now }));
    await waitFor(() => expect(result.current.dismissed).toEqual(['dismissed']));
    await waitFor(() => expect(result.current.mutedEverywhere).toEqual(['tips']));
    expect(result.current.visible.map((item) => item.NotificationId)).toEqual(['shown']);
  });

  it('stores a dismissal for this user, and does not count a dismissed one arriving again', async () => {
    const { client, state, send } = fakeClient(answersWith([notification('a')]));
    const { result } = renderHook(() => useMeetingNotifications('m1', { client }));
    await waitFor(() => expect(result.current.items).toHaveLength(1));
    await act(() => result.current.dismiss('a'));
    const update = state.requests.find((request) => request.query.includes('updateMyMeetingNotificationState('));
    expect(update.variables).toEqual({ input: { CallId: 'm1', Dismiss: ['a'] } });
    expect(result.current.visible).toHaveLength(0);
    act(() => send(notification('a', { Revision: 2 })));
    expect(result.current.liveArrivals).toBe(0);
  });

  it('puts a dismissal back when it cannot be saved', async () => {
    const answers = answersWith([notification('a')]);
    answers.updateMyMeetingNotificationState = new Error('no');
    const { client } = fakeClient(answers);
    const { result } = renderHook(() => useMeetingNotifications('m1', { client }));
    await waitFor(() => expect(result.current.items).toHaveLength(1));
    await act(() => result.current.dismiss('a'));
    expect(result.current.dismissed).toEqual([]);
    expect(result.current.error).toMatch(/could not be saved/);
  });
});

describe('MeetingNotificationsPanel', () => {
  afterEach(() => vi.restoreAllMocks());

  it('shows the feed in time order, with a heading for one without a title', async () => {
    const items = [
      notification('late', { CreatedAt: '2026-11-19T14:09:00.000Z' }),
      notification('early', { CreatedAt: '2026-11-19T14:01:00.000Z', Title: null }),
    ];
    const { client } = fakeClient(answersWith(items));
    render(<PanelWithHook client={client} />);
    await waitFor(() => expect(titles()).toEqual(['Guidance for this meeting', 'Title late']));
    expect(screen.getByText(/Never posted to the meeting chat/)).toBeTruthy();
  });

  it('renders Markdown without raw HTML or pictures', async () => {
    const body = '**bold** <img src="https://example.com/x.png"> ![p](https://example.com/y.png) <b>raw</b>';
    const { client } = fakeClient(answersWith([notification('a', { Body: body })]));
    const { container } = render(<PanelWithHook client={client} />);
    await waitFor(() => expect(container.querySelector('strong')?.textContent).toBe('bold'));
    expect(container.querySelector('img')).toBeNull();
    expect(container.querySelector('b')).toBeNull();
  });

  it('dismisses from its button and shows it again with Show dismissed', async () => {
    const { client } = fakeClient(answersWith([notification('a')]));
    render(<PanelWithHook client={client} />);
    await waitFor(() => expect(titles()).toEqual(['Title a']));
    await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Dismiss Title a' })));
    expect(titles()).toEqual([]);
    await act(async () => fireEvent.click(screen.getByRole('checkbox', { name: /Show dismissed/ })));
    expect(titles()).toEqual(['Title a']);
    expect(screen.getByRole('button', { name: 'Restore' })).toBeTruthy();
  });

  it('copies a COPY action text and shows no button for an action it does not know', async () => {
    const writeText = vi.fn(() => Promise.resolve());
    Object.assign(navigator, { clipboard: { writeText } });
    const actions = [
      { Label: 'Copy question', Kind: 'COPY', Payload: '{"text":"Who signs off?"}' },
      { Label: 'Do something', Kind: 'MUTATION', Payload: '{"action":"unknown","args":{}}' },
    ];
    const { client } = fakeClient(answersWith([notification('a', { Actions: actions })]));
    render(<PanelWithHook client={client} />);
    await waitFor(() => expect(screen.getByRole('button', { name: 'Copy question' })).toBeTruthy());
    expect(screen.queryByRole('button', { name: 'Do something' })).toBeNull();
    await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Copy question' })));
    expect(writeText).toHaveBeenCalledWith('Who signs off?');
  });
});

describe('MeetingNotificationsInline', () => {
  it('shows nothing until the meeting has a notification', async () => {
    const { client, send } = fakeClient(answersWith([]));
    render(<MeetingNotificationsInline callId="m1" client={client} />);
    expect(screen.queryByTestId('meeting-notifications')).toBeNull();
    act(() => send(notification('a')));
    expect(screen.getByTestId('meeting-notifications')).toBeTruthy();
  });
});

describe('notification actions', () => {
  it('ships with no mutation action', () => {
    expect(Object.keys(MUTATION_ACTIONS)).toEqual([]);
  });

  it('never takes a name every object has for an action', () => {
    const action = { Label: 'x', Kind: 'MUTATION', Payload: '{"action":"constructor"}' };
    expect(resolveAction(notification('a'), action)).toBeNull();
  });

  it('runs a listed action as its own operation, for the notification meeting', async () => {
    MUTATION_ACTIONS.test_action = {
      args: ['itemId'],
      query: 'mutation TestAction($callId: ID!, $itemId: ID!) { testAction }',
      variables: ({ callId, args }) => ({ callId, itemId: args.itemId }),
    };
    try {
      const action = {
        Label: 'Run',
        Kind: 'MUTATION',
        // A CallId in the arguments is not taken: the action does not accept it.
        Payload: '{"action":"test_action","args":{"itemId":"c1","callId":"other"}}',
      };
      expect(resolveAction(notification('a'), action)).toBeNull();
      const allowed = { ...action, Payload: '{"action":"test_action","args":{"itemId":"c1"}}' };
      const resolved = resolveAction(notification('a'), allowed);
      const client = { graphql: vi.fn(() => Promise.resolve({})) };
      await resolved.run(client);
      expect(client.graphql).toHaveBeenCalledWith({
        query: MUTATION_ACTIONS.test_action.query,
        variables: { callId: 'm1', itemId: 'c1' },
      });
    } finally {
      delete MUTATION_ACTIONS.test_action;
    }
  });

  it('shows at most two buttons and skips malformed ones', () => {
    const copy = (text) => ({ Label: text, Kind: 'COPY', Payload: JSON.stringify({ text }) });
    const actions = [{ Label: 'bad', Kind: 'COPY', Payload: 'not json' }, copy('one'), copy('two')];
    expect(notificationActions(notification('a', { Actions: actions })).map((a) => a.label)).toEqual(['one']);
  });
});

describe('meetingCallIdOf', () => {
  it('reads the meeting from a meeting page address only', () => {
    expect(meetingCallIdOf('/calls/Weekly%20sync')).toBe('Weekly sync');
    expect(meetingCallIdOf('/calls')).toBeNull();
    expect(meetingCallIdOf('/calls/query')).toBeNull();
    expect(meetingCallIdOf('/stream')).toBeNull();
  });
});
