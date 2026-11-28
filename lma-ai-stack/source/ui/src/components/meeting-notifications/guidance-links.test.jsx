/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * A guidance card's links to the meeting page: Ask the assistant (the card as
 * the question's context) and Add to whiteboard, each shown only while the
 * meeting page offers it for the card's meeting (common/meeting-actions.js).
 */
import React from 'react';
import { describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, renderHook, screen, waitFor } from '@testing-library/react';

import MeetingNotificationsPanel, { GUIDANCE_QUESTION } from './MeetingNotificationsPanel';
import useMeetingNotifications from '../../hooks/use-meeting-notifications';
import fakeClient, { notification } from './__mocks__/notification-client';
import {
  ADD_TO_WHITEBOARD,
  ASK_ASSISTANT,
  MAX_ACTION_TEXT,
  OPEN_WHITEBOARD,
  meetingActionAvailable,
  requestMeetingAction,
  useMeetingActions,
} from '../common/meeting-actions';

const answersWith = (items) => ({
  listMeetingNotifications: { Items: items, NextToken: null },
  getMyMeetingNotificationState: { CallId: 'm1', Dismissed: [], MutedCategories: [] },
  getMyNotificationPreferences: { MutedCategories: [] },
});

// eslint-disable-next-line react/prop-types
const Drawer = ({ client }) => {
  const notifications = useMeetingNotifications('m1', { client });
  return <MeetingNotificationsPanel notifications={notifications} />;
};

// eslint-disable-next-line react/prop-types
const MeetingPage = ({ ask, pin }) => {
  useMeetingActions('m1', { [ASK_ASSISTANT]: ask, [ADD_TO_WHITEBOARD]: pin });
  return null;
};

describe('meeting actions', () => {
  it('are carried out by the page that registered them, for its meeting only', () => {
    const open = vi.fn();
    const { unmount } = renderHook(() => useMeetingActions('m1', { [OPEN_WHITEBOARD]: open, [ASK_ASSISTANT]: null }));
    expect(meetingActionAvailable('m1', OPEN_WHITEBOARD)).toBe(true);
    expect(meetingActionAvailable('m1', ASK_ASSISTANT)).toBe(false);
    expect(meetingActionAvailable('m2', OPEN_WHITEBOARD)).toBe(false);
    expect(requestMeetingAction('m2', OPEN_WHITEBOARD, { whiteboardId: 'w' })).toBe(false);
    expect(requestMeetingAction('m1', OPEN_WHITEBOARD, { whiteboardId: 'w' })).toBe(true);
    expect(open).toHaveBeenCalledWith({ whiteboardId: 'w' });
    unmount();
    expect(meetingActionAvailable('m1', OPEN_WHITEBOARD)).toBe(false);
  });

  it('trim a long text', () => {
    const ask = vi.fn();
    renderHook(() => useMeetingActions('m1', { [ASK_ASSISTANT]: ask }));
    act(() => {
      requestMeetingAction('m1', ASK_ASSISTANT, { label: 'x', text: 'a'.repeat(MAX_ACTION_TEXT + 50) });
    });
    expect(ask.mock.calls[0][0].text).toHaveLength(MAX_ACTION_TEXT);
  });
});

describe('a guidance card', () => {
  it('asks the assistant with the card as context, and adds it to a whiteboard', async () => {
    const ask = vi.fn();
    const pin = vi.fn();
    const { client } = fakeClient(
      answersWith([notification('a', { Title: 'Ask about the budget', Body: 'Nobody named a number.' })]),
    );
    render(
      <>
        <MeetingPage ask={ask} pin={pin} />
        <Drawer client={client} />
      </>,
    );
    fireEvent.click(await screen.findByRole('button', { name: 'Ask the assistant' }));
    expect(ask).toHaveBeenCalledWith({
      label: 'Guidance: Ask about the budget',
      text: 'Ask about the budget\n\nNobody named a number.',
      question: GUIDANCE_QUESTION,
    });
    fireEvent.click(screen.getByRole('button', { name: 'Add to whiteboard' }));
    expect(pin).toHaveBeenCalledWith({ title: 'Ask about the budget', text: 'Nobody named a number.' });
  });

  it('shows the links only while the page offers them, and only on guidance', async () => {
    const { client } = fakeClient(answersWith([notification('a'), notification('b', { Category: 'system' })]));
    const { rerender } = render(
      <>
        <MeetingPage ask={null} pin={null} />
        <Drawer client={client} />
      </>,
    );
    await waitFor(() => expect(screen.getAllByTestId('meeting-notification')).toHaveLength(2));
    expect(screen.queryByRole('button', { name: 'Ask the assistant' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Add to whiteboard' })).toBeNull();
    rerender(
      <>
        <MeetingPage ask={() => {}} pin={() => {}} />
        <Drawer client={client} />
      </>,
    );
    await waitFor(() => expect(screen.getAllByRole('button', { name: 'Ask the assistant' })).toHaveLength(1));
    expect(screen.getAllByRole('button', { name: 'Add to whiteboard' })).toHaveLength(1);
  });
});
