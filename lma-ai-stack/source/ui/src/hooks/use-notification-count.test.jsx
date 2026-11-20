/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The count of meeting notifications in the top navigation: this visit only,
 * the user's own and shared meetings only, not the meeting already open, and
 * not a category muted everywhere.
 */
import { describe, expect, it } from 'vitest';
import { act, renderHook } from '@testing-library/react';

import useNotificationCount, { countsFor } from './use-notification-count';
import useNotificationsDrawer from './use-notifications-drawer';
import fakeClient, { notification } from '../components/meeting-notifications/__mocks__/notification-client';

describe('useNotificationCount', () => {
  it('subscribes to every meeting and counts each notification once', () => {
    const { client, state, send } = fakeClient();
    const { result } = renderHook(() => useNotificationCount({ username: 'owner@example.com', client }));
    expect(state.observers[0].variables).toEqual({});
    act(() => send(notification('a')));
    act(() => send(notification('a', { Revision: 2 })));
    act(() => send(notification('b', { CallId: 'm2' })));
    expect(result.current.total).toBe(2);
    expect(result.current.meetings.map((meeting) => meeting.callId).sort()).toEqual(['m1', 'm2']);
  });

  it('leaves out the open meeting, and clears a meeting when it is opened', () => {
    const { client, send } = fakeClient();
    const { result, rerender } = renderHook(
      ({ current }) => useNotificationCount({ username: 'owner@example.com', currentCallId: current, client }),
      { initialProps: { current: 'm1' } },
    );
    act(() => send(notification('a')));
    act(() => send(notification('b', { CallId: 'm2' })));
    expect(result.current.total).toBe(1);
    rerender({ current: 'm2' });
    expect(result.current.total).toBe(0);
  });

  it('counts only meetings the user owns or is shared on, and not muted categories', () => {
    expect(countsFor(notification('a'), 'owner@example.com')).toBe(true);
    expect(countsFor(notification('a'), 'shared@example.com')).toBe(true);
    // An Admin receives every meeting's messages; only their own meetings count.
    expect(countsFor(notification('a'), 'admin@example.com')).toBe(false);
    // A name that is part of an entry without being one.
    expect(countsFor(notification('a'), 'shared@example.co')).toBe(false);
    expect(countsFor(notification('a'), 'owner@example.com', ['guidance'])).toBe(false);
    expect(countsFor(notification('a'), null)).toBe(false);
  });
});

const memoryStorage = () => {
  const values = {};
  return {
    getItem: (key) => values[key] ?? null,
    setItem: (key, value) => {
      values[key] = value;
    },
  };
};

describe('useNotificationsDrawer', () => {
  it('opens on the first arrival, and after the user closes it marks the trigger instead', () => {
    const storage = memoryStorage();
    const { result, rerender } = renderHook(
      ({ arrivals }) => useNotificationsDrawer({ callId: 'm1', liveArrivals: arrivals, storage }),
      { initialProps: { arrivals: 0 } },
    );
    expect(result.current.open).toBe(false);
    rerender({ arrivals: 1 });
    expect(result.current.open).toBe(true);
    act(() => result.current.setOpen(false));
    rerender({ arrivals: 2 });
    expect(result.current.open).toBe(false);
    expect(result.current.badge).toBe(true);
    act(() => result.current.setOpen(true));
    expect(result.current.badge).toBe(false);
  });

  it('remembers its width', () => {
    const storage = memoryStorage();
    const { result } = renderHook(() => useNotificationsDrawer({ callId: 'm1', liveArrivals: 0, storage }));
    act(() => result.current.resize(480));
    const again = renderHook(() => useNotificationsDrawer({ callId: 'm1', liveArrivals: 0, storage }));
    expect(again.result.current.size).toBe(480);
  });
});

describe('useNotificationsDrawer, put away for Help', () => {
  it('still opens by itself after Help took its place', () => {
    const storage = memoryStorage();
    const { result, rerender } = renderHook(
      ({ arrivals }) => useNotificationsDrawer({ callId: 'm1', liveArrivals: arrivals, storage }),
      { initialProps: { arrivals: 0 } },
    );
    rerender({ arrivals: 1 });
    expect(result.current.open).toBe(true);
    act(() => result.current.hide());
    expect(result.current.open).toBe(false);
    rerender({ arrivals: 2 });
    expect(result.current.open).toBe(true);
    expect(result.current.badge).toBe(false);
  });
});

describe('useNotificationsDrawer, with Help open', () => {
  it('marks the trigger instead of opening behind Help', () => {
    const storage = memoryStorage();
    const { result, rerender } = renderHook(
      ({ arrivals, covered }) => useNotificationsDrawer({ callId: 'm1', liveArrivals: arrivals, covered, storage }),
      { initialProps: { arrivals: 0, covered: true } },
    );
    rerender({ arrivals: 1, covered: true });
    expect(result.current.open).toBe(false);
    expect(result.current.badge).toBe(true);
    act(() => result.current.setOpen(true));
    expect(result.current.badge).toBe(false);
  });
});
