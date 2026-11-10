/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Whether the meeting assistant's panel starts open, and what is remembered:
 * the user's own choice, per user, and the live meetings they have seen.
 */
import { act, renderHook } from '@testing-library/react';
import { beforeEach, describe, expect, it } from 'vitest';

import useAssistantPanel, {
  MAX_SEEN_LIVE,
  OPEN_STORAGE_KEY,
  SEEN_LIVE_STORAGE_KEY,
  initialAssistantOpen,
  rememberLiveMeeting,
  resolveAssistantOpen,
  storeAssistantOpen,
  storedAssistantOpen,
} from './use-assistant-panel';

beforeEach(() => window.localStorage.clear());

describe('the remembered choice', () => {
  it('is kept per user', () => {
    storeAssistantOpen('alice', true);
    storeAssistantOpen('bob', false);
    expect(storedAssistantOpen('alice')).toBe(true);
    expect(storedAssistantOpen('bob')).toBe(false);
    expect(storedAssistantOpen('carol')).toBeNull();
    expect(window.localStorage.getItem(`${OPEN_STORAGE_KEY}:alice`)).toBe('true');
  });

  it('ignores a stored value it does not recognise', () => {
    window.localStorage.setItem(`${OPEN_STORAGE_KEY}:alice`, 'maybe');
    window.localStorage.setItem(`${SEEN_LIVE_STORAGE_KEY}:alice`, '{not json');
    expect(storedAssistantOpen('alice')).toBeNull();
    expect(initialAssistantOpen({ callId: 'm-1', isLive: true, username: 'alice' })).toBe(true);
  });
});

describe('initialAssistantOpen', () => {
  it('opens a live meeting the user has not seen, whatever they chose before', () => {
    storeAssistantOpen('alice', false);
    expect(initialAssistantOpen({ callId: 'm-1', isLive: true, username: 'alice' })).toBe(true);
  });

  it('follows the remembered choice for a live meeting already seen', () => {
    rememberLiveMeeting('alice', 'm-1');
    expect(initialAssistantOpen({ callId: 'm-1', isLive: true, username: 'alice' })).toBeNull();
    storeAssistantOpen('alice', false);
    expect(initialAssistantOpen({ callId: 'm-1', isLive: true, username: 'alice' })).toBe(false);
    storeAssistantOpen('alice', true);
    expect(initialAssistantOpen({ callId: 'm-1', isLive: true, username: 'alice' })).toBe(true);
  });

  it('leaves an ended meeting undecided until the user has made a choice', () => {
    expect(initialAssistantOpen({ callId: 'm-1', isLive: false, username: 'alice' })).toBeNull();
    storeAssistantOpen('alice', true);
    expect(initialAssistantOpen({ callId: 'm-1', isLive: false, username: 'alice' })).toBe(true);
  });

  it('with no choice made, is open only where the panel sits beside the tabs', () => {
    expect(resolveAssistantOpen(null, true)).toBe(true);
    expect(resolveAssistantOpen(null, false)).toBe(false);
    // Not measured yet.
    expect(resolveAssistantOpen(null, null)).toBe(false);
    expect(resolveAssistantOpen(false, true)).toBe(false);
    expect(resolveAssistantOpen(true, false)).toBe(true);
  });

  it('remembers a bounded number of live meetings, forgetting the oldest', () => {
    for (let i = 0; i <= MAX_SEEN_LIVE; i += 1) rememberLiveMeeting('alice', `m-${i}`);
    const seen = JSON.parse(window.localStorage.getItem(`${SEEN_LIVE_STORAGE_KEY}:alice`));
    expect(seen).toHaveLength(MAX_SEEN_LIVE);
    expect(seen).not.toContain('m-0');
    expect(seen).toContain(`m-${MAX_SEEN_LIVE}`);
  });
});

describe('useAssistantPanel', () => {
  const render = (props) =>
    renderHook((p) => useAssistantPanel(p), {
      initialProps: { enabled: true, callId: 'm-1', isLive: false, username: 'alice', ...props },
    });

  it('is closed and never created when the assistant is not enabled', () => {
    const { result } = render({ enabled: false, isLive: true });
    expect(result.current).toMatchObject({ open: false, mounted: false });
  });

  it('follows the page width until the user chooses, creating the frame the first time it is open', () => {
    const { result, rerender } = render({ besideTabs: false });
    expect(result.current).toMatchObject({ open: false, mounted: false });
    rerender({ enabled: true, callId: 'm-1', isLive: false, username: 'alice', besideTabs: true });
    expect(result.current).toMatchObject({ open: true, mounted: true });
    rerender({ enabled: true, callId: 'm-1', isLive: false, username: 'alice', besideTabs: false });
    expect(result.current).toMatchObject({ open: false, mounted: true });
    act(() => result.current.setOpen(true));
    rerender({ enabled: true, callId: 'm-1', isLive: false, username: 'alice', besideTabs: false });
    expect(result.current.open).toBe(true);
  });

  it('remembers the choice the user makes', () => {
    const { result } = render();
    expect(result.current).toMatchObject({ open: false, mounted: false });
    act(() => result.current.setOpen(true));
    expect(result.current).toMatchObject({ open: true, mounted: true });
    expect(storedAssistantOpen('alice')).toBe(true);
    act(() => result.current.setOpen(false));
    // Closed, but the chat frame it created is kept.
    expect(result.current).toMatchObject({ open: false, mounted: true });
    expect(storedAssistantOpen('alice')).toBe(false);
  });

  it('does not remember a closing made for the viewer rather than by them', () => {
    storeAssistantOpen('alice', true);
    const { result } = render();
    act(() => result.current.setOpen(false, { remember: false }));
    expect(result.current.open).toBe(false);
    expect(storedAssistantOpen('alice')).toBe(true);
  });

  it('records a live meeting as seen, so it opens by itself only the first time', () => {
    const first = render({ isLive: true });
    expect(first.result.current.open).toBe(true);
    act(() => first.result.current.setOpen(false));
    first.unmount();
    const again = render({ isLive: true });
    expect(again.result.current.open).toBe(false);
  });

  it('decides again for another meeting shown in the same place, keeping the frame', () => {
    const { result, rerender } = render({ isLive: true });
    expect(result.current).toMatchObject({ open: true, mounted: true });
    rerender({ enabled: true, callId: 'm-2', isLive: false, username: 'alice' });
    expect(result.current).toMatchObject({ open: false, mounted: true });
  });

  it("opens as a host asks, without changing the user's remembered choice", () => {
    storeAssistantOpen('alice', false);
    const { result } = render({ defaultOpen: true });
    expect(result.current.open).toBe(true);
    act(() => result.current.setOpen(false));
    act(() => result.current.setOpen(true));
    expect(storedAssistantOpen('alice')).toBe(false);
  });
});
