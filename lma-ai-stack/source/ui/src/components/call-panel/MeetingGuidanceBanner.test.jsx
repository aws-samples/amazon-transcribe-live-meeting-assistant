/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The guidance banner: what an MCP client pushes to the meeting appears on the
 * meeting page, each note dismissible, from the onAddMeetingGuidance
 * subscription for this meeting only.
 */
import React from 'react';
import { describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';

import MeetingGuidanceBanner from './MeetingGuidanceBanner';
import { MAX_GUIDANCE_ITEMS } from '../../hooks/use-meeting-guidance';

const fakeClient = () => {
  const handlers = {};
  const unsubscribe = vi.fn();
  const client = {
    graphql: vi.fn(({ query, variables }) => {
      handlers.query = query;
      handlers.variables = variables;
      return {
        subscribe: (observer) => {
          handlers.next = observer.next;
          return { unsubscribe };
        },
      };
    }),
  };
  return { client, handlers, unsubscribe };
};

const guidance = (id, extra = {}) => ({
  data: {
    onAddMeetingGuidance: {
      CallId: 'm1',
      GuidanceId: id,
      Title: `Title ${id}`,
      Text: `Text ${id}`,
      Level: 'INFO',
      CreatedBy: 'agent@example.com',
      CreatedAt: '2026-11-19T14:00:00.000Z',
      Owner: 'owner@example.com',
      SharedWith: '[shared@example.com]',
      ...extra,
    },
  },
});

describe('MeetingGuidanceBanner', () => {
  it('subscribes to this meeting, selecting the fields its access filter needs', () => {
    const { client, handlers } = fakeClient();
    render(<MeetingGuidanceBanner callId="m1" client={client} />);
    expect(handlers.variables).toEqual({ callId: 'm1' });
    expect(handlers.query).toContain('onAddMeetingGuidance(CallId: $callId)');
    expect(handlers.query).toMatch(/\bOwner\b/);
    expect(handlers.query).toMatch(/\bSharedWith\b/);
  });

  it('shows nothing until guidance arrives, then shows it', () => {
    const { client, handlers } = fakeClient();
    const { container } = render(<MeetingGuidanceBanner callId="m1" client={client} />);
    expect(container).toBeEmptyDOMElement();
    act(() => handlers.next(guidance('g1', { Text: 'Ask about the renewal date' })));
    expect(screen.getByText('Title g1')).toBeInTheDocument();
    expect(screen.getByText('Ask about the renewal date')).toBeInTheDocument();
    expect(screen.getByText(/From agent@example.com, through an MCP client/)).toBeInTheDocument();
  });

  it('removes a note when it is dismissed and keeps the others', async () => {
    const { client, handlers } = fakeClient();
    render(<MeetingGuidanceBanner callId="m1" client={client} />);
    act(() => {
      handlers.next(guidance('g1'));
      handlers.next(guidance('g2'));
    });
    const buttons = screen.getAllByRole('button', { name: 'Dismiss guidance' });
    expect(buttons).toHaveLength(2);
    // The newest is first.
    fireEvent.click(buttons[0]);
    // The Flashbar animates a dismissed item out before removing it.
    await waitFor(() => expect(screen.queryByText('Text g2')).not.toBeInTheDocument());
    expect(screen.getByText('Text g1')).toBeInTheDocument();
  });

  it('ignores a message for another meeting and a repeated one', () => {
    const { client, handlers } = fakeClient();
    render(<MeetingGuidanceBanner callId="m1" client={client} />);
    act(() => {
      handlers.next(guidance('g1'));
      handlers.next(guidance('g1'));
      handlers.next(guidance('g9', { CallId: 'm2' }));
    });
    expect(screen.getAllByText('Text g1')).toHaveLength(1);
    expect(screen.queryByText('Text g9')).not.toBeInTheDocument();
  });

  it('keeps the newest few notes', () => {
    const { client, handlers } = fakeClient();
    render(<MeetingGuidanceBanner callId="m1" client={client} />);
    act(() => {
      for (let index = 0; index <= MAX_GUIDANCE_ITEMS; index += 1) handlers.next(guidance(`g${index}`));
    });
    expect(screen.getAllByRole('button', { name: 'Dismiss guidance' })).toHaveLength(MAX_GUIDANCE_ITEMS);
    expect(screen.queryByText('Text g0')).not.toBeInTheDocument();
  });

  it('shows guidance text as text, not markup', () => {
    const { client, handlers } = fakeClient();
    const { container } = render(<MeetingGuidanceBanner callId="m1" client={client} />);
    act(() => handlers.next(guidance('g1', { Text: 'The <b>renewal</b> date' })));
    expect(screen.getByText('The <b>renewal</b> date')).toBeInTheDocument();
    expect(container.querySelector('b')).toBeNull();
  });

  it('unsubscribes when the page leaves the meeting', () => {
    const { client, unsubscribe } = fakeClient();
    const { unmount } = render(<MeetingGuidanceBanner callId="m1" client={client} />);
    unmount();
    expect(unsubscribe).toHaveBeenCalled();
  });
});
