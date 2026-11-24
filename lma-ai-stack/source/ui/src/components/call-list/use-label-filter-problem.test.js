/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { LABEL_FILTER_KEY_PROBLEM } from '../common/meeting-preparation';
import useLabelFilterProblem, { LABEL_PROBLEM_DELAY_MS } from './use-label-filter-problem';

describe('the meeting list label filter message', () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  const typed = (initial) => renderHook(({ text }) => useLabelFilterProblem(text), { initialProps: { text: initial } });

  it('says why a key matches nothing only once typing pauses', () => {
    const hook = typed('P');
    expect(hook.result.current).toBeNull();
    act(() => vi.advanceTimersByTime(LABEL_PROBLEM_DELAY_MS - 100));
    hook.rerender({ text: 'Pr' });
    act(() => vi.advanceTimersByTime(LABEL_PROBLEM_DELAY_MS - 100));
    expect(hook.result.current).toBeNull();
    act(() => vi.advanceTimersByTime(100));
    expect(hook.result.current).toBe(LABEL_FILTER_KEY_PROBLEM);
  });

  it('clears the message as soon as the filter can match', () => {
    const hook = typed('Project');
    act(() => vi.advanceTimersByTime(LABEL_PROBLEM_DELAY_MS));
    expect(hook.result.current).toBe(LABEL_FILTER_KEY_PROBLEM);
    hook.rerender({ text: 'project' });
    expect(hook.result.current).toBeNull();
  });

  it('shows nothing for a filter that can match, or none', () => {
    ['', 'project=acme', 'customer=  Opaque X '].forEach((text) => {
      const hook = typed(text);
      act(() => vi.advanceTimersByTime(LABEL_PROBLEM_DELAY_MS * 2));
      expect(hook.result.current).toBeNull();
    });
  });
});
