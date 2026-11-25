/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The Participants section of a Virtual Participant's page: editable by the
 * owner and Admins at any status, showing and changing the Virtual
 * Participant's own list until its meeting exists and the meeting's list from
 * then on, and offering the invitation to consent whenever the list has people.
 */
import React from 'react';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import VpParticipants, { MEETING_LOOKUP_INTERVAL_MS } from './VpParticipants';

const { graphqlMock } = vi.hoisted(() => ({ graphqlMock: vi.fn() }));
vi.mock('aws-amplify/api', () => ({ generateClient: () => ({ graphql: graphqlMock }) }));
vi.mock('aws-amplify/utils', () => ({
  ConsoleLogger: class {
    error() {} // eslint-disable-line class-methods-use-this

    debug() {} // eslint-disable-line class-methods-use-this
  },
}));

const OWNER = 'owner@example.com';
const ANA = { name: 'Ana Lima', email: 'ana@example.com' };
const CY = { name: 'Cy', email: 'cy@example.net' };

const vpOf = (overrides = {}) => ({
  id: 'vp-1',
  Owner: OWNER,
  status: 'SCHEDULED',
  meetingName: 'Weekly sync',
  participants: [ANA],
  ...overrides,
});

/** The server: `meeting` is the meeting record (null before it exists); `saved` what the write answers. */
const serve = ({ meeting = null, saved = null } = {}) => {
  graphqlMock.mockReset();
  graphqlMock.mockImplementation(({ query }) => {
    if (query.includes('getCall(')) return Promise.resolve({ data: { getCall: meeting } });
    if (query.includes('updateCallParticipants(')) return Promise.resolve({ data: { updateCallParticipants: saved } });
    if (query.includes('getMeetingParticipantAccounts(')) {
      return Promise.resolve({ data: { getMeetingParticipantAccounts: [] } });
    }
    return Promise.reject(new Error(`unexpected ${query}`));
  });
};

const calls = (name) => graphqlMock.mock.calls.map(([c]) => c).filter((c) => c.query.includes(`${name}(`));

const saveList = async (text) => {
  fireEvent.click(screen.getByText('Edit'));
  fireEvent.change(screen.getByLabelText('Participants, one Name <email> per line'), { target: { value: text } });
  fireEvent.click(within(screen.getByTestId('meeting-participants')).getByText('Save').closest('button'));
};

describe('VpParticipants', () => {
  beforeEach(() => serve());

  it.each(['SCHEDULED', 'JOINING', 'ACTIVE', 'COMPLETED'])(
    'lets the owner change the list of a Virtual Participant that is %s',
    (status) => {
      render(<VpParticipants vp={vpOf({ status })} username={OWNER} />);
      expect(screen.getByText('Edit')).toBeTruthy();
      expect(screen.getByText('Invite participants to consent')).toBeTruthy();
    },
  );

  it('lets an Admin change it, but not a user who does not own it', () => {
    const { unmount } = render(<VpParticipants vp={vpOf()} username="admin@example.com" isAdmin />);
    expect(screen.getByText('Edit')).toBeTruthy();
    unmount();
    render(<VpParticipants vp={vpOf()} username="other@example.com" />);
    expect(screen.queryByText('Edit')).toBeNull();
    expect(screen.queryByText('Invite participants to consent')).toBeNull();
  });

  it('changes the Virtual Participant’s own list before its meeting exists', async () => {
    serve({ saved: null });
    const onVpListSaved = vi.fn();
    render(<VpParticipants vp={vpOf()} username={OWNER} onVpListSaved={onVpListSaved} />);
    expect(screen.getByText(/puts this list on the meeting when it creates it/)).toBeTruthy();
    expect(calls('getCall')).toHaveLength(0);
    await saveList('Ana Lima <ana@example.com>\nCy <cy@example.net>');
    await waitFor(() => expect(onVpListSaved).toHaveBeenCalledWith([ANA, CY]));
    expect(calls('updateCallParticipants')[0].variables).toEqual({
      input: { VirtualParticipantId: 'vp-1', Participants: [ANA, CY] },
    });
  });

  it('shows and changes the meeting’s list once the meeting exists', async () => {
    serve({
      meeting: { CallId: 'Weekly sync-1', Owner: OWNER, Participants: [ANA, CY] },
      saved: { CallId: 'Weekly sync-1', Owner: OWNER, Participants: [CY] },
    });
    const onVpListSaved = vi.fn();
    render(
      <VpParticipants
        vp={vpOf({ status: 'ACTIVE', CallId: 'Weekly sync-1' })}
        username={OWNER}
        onVpListSaved={onVpListSaved}
      />,
    );
    // The meeting's list, not the one the Virtual Participant started with.
    expect(await screen.findByText('cy@example.net')).toBeTruthy();
    expect(screen.getByText(/the same one its meeting page shows/)).toBeTruthy();
    expect(calls('getCall')[0].variables).toEqual({ CallId: 'Weekly sync-1' });
    await saveList('Cy <cy@example.net>');
    await waitFor(() => expect(screen.queryByText('ana@example.com')).toBeNull());
    expect(calls('updateCallParticipants')[0].variables.input.VirtualParticipantId).toBe('vp-1');
    expect(onVpListSaved).not.toHaveBeenCalled();
  });

  it('keeps showing the Virtual Participant’s list while its meeting record does not exist yet', async () => {
    serve({ meeting: null });
    render(<VpParticipants vp={vpOf({ status: 'JOINING', CallId: 'Weekly sync-1' })} username={OWNER} />);
    await waitFor(() => expect(calls('getCall')).toHaveLength(1));
    expect(screen.getByText('ana@example.com')).toBeTruthy();
    expect(screen.getByText(/puts this list on the meeting when it creates it/)).toBeTruthy();
  });

  it('switches to the meeting’s list when the record appears after the CallId, with no status change', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      let meeting = null;
      graphqlMock.mockReset();
      graphqlMock.mockImplementation(({ query }) => {
        if (query.includes('getCall(')) return Promise.resolve({ data: { getCall: meeting } });
        return Promise.resolve({ data: { getMeetingParticipantAccounts: [] } });
      });
      render(<VpParticipants vp={vpOf({ status: 'JOINED', CallId: 'Weekly sync-1' })} username={OWNER} />);
      await waitFor(() => expect(calls('getCall')).toHaveLength(1));
      expect(screen.queryByText('cy@example.net')).toBeNull();
      meeting = { CallId: 'Weekly sync-1', Owner: OWNER, Participants: [ANA, CY] };
      await vi.advanceTimersByTimeAsync(MEETING_LOOKUP_INTERVAL_MS);
      expect(await screen.findByText('cy@example.net')).toBeTruthy();
      expect(screen.getByText(/the same one its meeting page shows/)).toBeTruthy();
      // Found: no more lookups.
      const looked = calls('getCall').length;
      await vi.advanceTimersByTimeAsync(MEETING_LOOKUP_INTERVAL_MS * 3);
      expect(calls('getCall')).toHaveLength(looked);
    } finally {
      vi.useRealTimers();
    }
  });

  it('offers Add participants to a Virtual Participant with no list', () => {
    render(<VpParticipants vp={vpOf({ participants: [] })} username={OWNER} />);
    expect(screen.getAllByText('Add participants').length).toBeGreaterThan(0);
    expect(screen.queryByText('Invite participants to consent')).toBeNull();
  });
});
