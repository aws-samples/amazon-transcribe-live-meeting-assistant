/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import { describe, it, expect, vi } from 'vitest';
import { renderHook, waitFor } from '@testing-library/react';

import useSummaryProfiles, { DEFAULT_SUMMARY_PROFILE, summaryProfileOptions } from './use-summary-profiles';

const LISTED = [
  DEFAULT_SUMMARY_PROFILE,
  { Name: 'discovery-session', Label: 'Discovery session', Description: 'Workshops' },
];

describe('useSummaryProfiles', () => {
  it('reads the profiles once enabled', async () => {
    const client = { graphql: vi.fn().mockResolvedValue({ data: { listSummaryProfiles: LISTED } }) };
    const { result } = renderHook(() => useSummaryProfiles(true, { client }));
    await waitFor(() => expect(result.current).toEqual(LISTED));
    expect(client.graphql).toHaveBeenCalledTimes(1);
  });

  it('offers the default alone when the profiles cannot be read, or until it is enabled', async () => {
    const failing = { graphql: vi.fn().mockRejectedValue(new Error('denied')) };
    const { result } = renderHook(() => useSummaryProfiles(true, { client: failing }));
    await waitFor(() => expect(failing.graphql).toHaveBeenCalled());
    expect(result.current).toEqual([DEFAULT_SUMMARY_PROFILE]);
    const idle = { graphql: vi.fn() };
    const { result: off } = renderHook(() => useSummaryProfiles(false, { client: idle }));
    expect(off.current).toEqual([DEFAULT_SUMMARY_PROFILE]);
    expect(idle.graphql).not.toHaveBeenCalled();
  });
});

describe('summaryProfileOptions', () => {
  it('offers each profile by its label', () => {
    expect(summaryProfileOptions(LISTED, 'default').map((o) => [o.value, o.label])).toEqual([
      ['default', 'Default'],
      ['discovery-session', 'Discovery session'],
    ]);
  });

  it('keeps a stored profile the configuration no longer holds, so it shows', () => {
    const options = summaryProfileOptions(LISTED, 'retired-profile');
    expect(options[2]).toEqual({
      value: 'retired-profile',
      label: 'retired-profile',
      description: 'Not in the configuration',
    });
  });
});
