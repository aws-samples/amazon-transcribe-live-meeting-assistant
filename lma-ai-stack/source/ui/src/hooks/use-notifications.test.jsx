/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/** The welcome message is shown on the first visit only, and its dismissal is kept. */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, waitFor, act } from '@testing-library/react';

vi.mock('../contexts/app', () => ({
  default: () => ({ errorMessage: '', setErrorMessage: vi.fn() }),
}));

// eslint-disable-next-line import/first
import useNotifications from './use-notifications';

const welcome = (notifications) => notifications.find((n) => n.id === 'welcome-1');

describe('useNotifications', () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it('shows the welcome on the first visit and not on the next', async () => {
    const first = renderHook(() => useNotifications());
    await waitFor(() => expect(welcome(first.result.current)).toBeTruthy());
    first.unmount();

    const next = renderHook(() => useNotifications());
    // Give the effect its turn before asserting that nothing was added.
    await act(async () => {});
    expect(welcome(next.result.current)).toBeUndefined();
  });

  it('can still be dismissed during the first visit', async () => {
    const { result } = renderHook(() => useNotifications());
    await waitFor(() => expect(welcome(result.current)).toBeTruthy());
    act(() => welcome(result.current).onDismiss());
    await waitFor(() => expect(welcome(result.current)).toBeUndefined());
    expect(JSON.parse(localStorage.getItem('dismissedInitialNotifications'))).toEqual(['welcome-1']);
  });
});
