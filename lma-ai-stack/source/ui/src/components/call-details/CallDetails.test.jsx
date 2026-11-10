/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The meeting page's copy of the meeting follows the meeting: it is replaced when
 * the meeting starts or ends, whatever UpdatedAt says, and can be read again on
 * request -- the Speakers panel asks when the backend says the meeting has ended
 * while the page still shows it in progress.
 */
import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { act, render, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';

import CallDetails from './CallDetails';

const panel = vi.hoisted(() => ({ props: null }));
const context = vi.hoisted(() => ({ value: null }));

vi.mock('../call-panel', () => ({
  default: (props) => {
    panel.props = props;
    return <div data-testid="panel">{props.item.recordingStatusLabel}</div>;
  },
}));
vi.mock('../../contexts/calls', () => ({ default: () => context.value }));

const CALL_ID = 'weekly-sync-2026-11-03-14-05-09-123';
const meeting = (fields = {}) => ({
  CallId: CALL_ID,
  Status: 'STARTED',
  UpdatedAt: '2026-11-03T14:30:00.000Z',
  CreatedAt: '2026-11-03T14:05:09.123Z',
  ...fields,
});

const renderPage = (calls = []) => {
  const getCallDetailsFromCallIds = vi.fn().mockResolvedValue([meeting()]);
  context.value = {
    calls,
    callTranscriptPerCallId: { [CALL_ID]: {} },
    getCallDetailsFromCallIds,
    sendGetTranscriptSegmentsRequest: vi.fn(),
    setToolsOpen: vi.fn(),
    setLiveTranscriptCallId: vi.fn(),
  };
  // A new element each time: React skips re-rendering an identical one.
  const view = () => (
    <MemoryRouter initialEntries={[`/calls/${encodeURIComponent(CALL_ID)}`]}>
      <Routes>
        <Route path="/calls/:callId" element={<CallDetails />} />
      </Routes>
    </MemoryRouter>
  );
  const result = render(view());
  return { ...result, view, getCallDetailsFromCallIds };
};

beforeEach(() => {
  panel.props = null;
});

describe('the meeting page follows the meeting', () => {
  it('takes up an ended meeting even when its UpdatedAt has not moved', async () => {
    const { rerender, view, getByTestId } = renderPage();
    await waitFor(() => expect(getByTestId('panel').textContent).toBe('In Progress'));
    context.value = { ...context.value, calls: [meeting({ Status: 'ENDED' })] };
    rerender(view());
    await waitFor(() => expect(getByTestId('panel').textContent).toBe('Done'));
  });

  it('is not taken off "in progress" by an update that carries no status', async () => {
    const { rerender, view, getByTestId } = renderPage();
    await waitFor(() => expect(getByTestId('panel').textContent).toBe('In Progress'));
    context.value = { ...context.value, calls: [meeting({ Status: null })] };
    rerender(view());
    await waitFor(() => expect(getByTestId('panel').textContent).toBe('In Progress'));
  });

  it('reads the meeting again when a part of the page asks', async () => {
    const { getByTestId, getCallDetailsFromCallIds } = renderPage();
    await waitFor(() => expect(getByTestId('panel').textContent).toBe('In Progress'));
    getCallDetailsFromCallIds.mockResolvedValue([meeting({ Status: 'ENDED' })]);
    await act(() => panel.props.onRefreshMeeting());
    expect(getCallDetailsFromCallIds).toHaveBeenLastCalledWith([CALL_ID]);
    expect(getByTestId('panel').textContent).toBe('Done');
  });

  it('keeps a re-read ended meeting when an older in-progress copy arrives', async () => {
    const { rerender, view, getByTestId, getCallDetailsFromCallIds } = renderPage();
    await waitFor(() => expect(getByTestId('panel').textContent).toBe('In Progress'));
    getCallDetailsFromCallIds.mockResolvedValue([meeting({ Status: 'ENDED', UpdatedAt: '2026-11-03T15:00:00.000Z' })]);
    await act(() => panel.props.onRefreshMeeting());
    expect(getByTestId('panel').textContent).toBe('Done');
    context.value = { ...context.value, calls: [meeting()] };
    rerender(view());
    await waitFor(() => expect(getByTestId('panel').textContent).toBe('Done'));
  });
});
