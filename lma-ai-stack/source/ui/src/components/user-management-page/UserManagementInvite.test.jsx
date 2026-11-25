/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The Create user form's optional "Meeting to invite them to": the meetings it
 * offers, what a pasted link is read as, and that createUser is sent the id
 * only when one is given.
 */
import React from 'react';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import UserManagementPage from './UserManagementPage';
import { invitableMeetingOptions, meetingRefFromText } from './invite-meeting';

const { graphqlMock } = vi.hoisted(() => ({ graphqlMock: vi.fn() }));
vi.mock('aws-amplify/api', () => ({ generateClient: () => ({ graphql: graphqlMock }) }));

const VPS = [
  { id: 'vp-1', CallId: '', meetingName: 'Planning', status: 'SCHEDULED', scheduledFor: '2026-11-26T10:00:00Z' },
  { id: 'vp-2', CallId: 'Standup-1', meetingName: 'Standup', status: 'ACTIVE', createdAt: '2026-11-25T09:00:00Z' },
  { id: 'vp-3', CallId: 'Retro-1', meetingName: 'Retro', status: 'COMPLETED', createdAt: '2026-11-20T09:00:00Z' },
];
const CALLS = [
  { CallId: 'Standup-1', CreatedAt: '2026-11-25T09:01:00Z' },
  { CallId: 'Retro-1', CreatedAt: '2026-11-20T09:01:00Z' },
  { CallId: 'Stream 2026-11-24', CreatedAt: '2026-11-24T15:00:00Z' },
];

const calls = (name) => graphqlMock.mock.calls.map(([c]) => c).filter((c) => c.query.includes(`${name}`));

beforeEach(() => {
  graphqlMock.mockReset();
  graphqlMock.mockImplementation(({ query }) => {
    if (query.includes('query ListUsers')) {
      return Promise.resolve({ data: { listUsers: { users: [], lastSignInAvailable: true } } });
    }
    if (query.includes('query ListInvitableMeetings')) {
      return Promise.resolve({ data: { listVirtualParticipants: VPS, listCallsDateRange: { Calls: CALLS } } });
    }
    if (query.includes('mutation CreateUser')) {
      return Promise.resolve({ data: { createUser: { username: 'new@example.com', email: 'new@example.com' } } });
    }
    return Promise.resolve({ data: {} });
  });
});

const openForm = async () => {
  render(<UserManagementPage />);
  fireEvent.click(await screen.findByText('Create user', { selector: 'button *' }));
  const dialog = await screen.findByRole('dialog', { name: 'Create user' });
  fireEvent.change(within(dialog).getByPlaceholderText('user@example.com'), {
    target: { value: 'new@example.com' },
  });
  return dialog;
};

const submit = (dialog) => fireEvent.click(within(dialog).getByText('Create', { selector: 'button *' }));

describe('Create user: the meeting to invite them to', () => {
  it('is optional: without one, createUser is sent the email and role only', async () => {
    const dialog = await openForm();
    submit(dialog);
    await waitFor(() => expect(calls('mutation CreateUser')).toHaveLength(1));
    expect(calls('mutation CreateUser')[0].variables).toEqual({ input: { email: 'new@example.com', role: 'User' } });
    expect(await screen.findByText(/An invitation email with a temporary password has been sent\./)).toBeTruthy();
  });

  it('lists the meetings to choose from when the form opens', async () => {
    await openForm();
    await waitFor(() => expect(calls('query ListInvitableMeetings')).toHaveLength(1));
    const { variables } = calls('query ListInvitableMeetings')[0];
    expect(new Date(variables.endDateTime) > new Date(variables.startDateTime)).toBe(true);
  });

  it('sends the id read from a pasted consent link, and says the invitation carries the link', async () => {
    const dialog = await openForm();
    fireEvent.change(within(dialog).getByLabelText('Meeting to invite them to'), {
      target: { value: 'https://d111.cloudfront.net/#/consents?vp=vp-1' },
    });
    submit(dialog);
    await waitFor(() => expect(calls('mutation CreateUser')).toHaveLength(1));
    expect(calls('mutation CreateUser')[0].variables).toEqual({
      input: { email: 'new@example.com', role: 'User', inviteToMeeting: 'vp-1' },
    });
    expect(await screen.findByText(/the meeting's consent link has been sent/)).toBeTruthy();
  });
});

describe('meetingRefFromText', () => {
  it.each([
    ['', ''],
    ['  vp-1  ', 'vp-1'],
    ['https://x.cloudfront.net/#/consents?meeting=Weekly%20sync', 'Weekly sync'],
    ['https://x.cloudfront.net/#/consents?vp=vp-1', 'vp-1'],
    ['https://x.cloudfront.net/#/calls/Weekly%20sync%20-%202026', 'Weekly sync - 2026'],
    ['https://x.cloudfront.net/#/virtual-participant/vp-9', 'vp-9'],
    ['Stream 2026-11-24', 'Stream 2026-11-24'],
  ])('reads %j as %j', (text, expected) => {
    expect(meetingRefFromText(text)).toBe(expected);
  });
});

describe('invitableMeetingOptions', () => {
  it('offers the Virtual Participants still to run or running, then the other recent meetings, newest first', () => {
    const groups = invitableMeetingOptions(VPS, CALLS);
    expect(groups.map((g) => g.label)).toEqual(['Virtual Participants', 'Recent meetings']);
    expect(groups[0].options.map((o) => o.value)).toEqual(['vp-1', 'vp-2']);
    // Standup-1 is offered as its Virtual Participant; a finished one's meeting is offered itself.
    expect(groups[1].options.map((o) => o.value)).toEqual(['Stream 2026-11-24', 'Retro-1']);
  });

  it('offers nothing when there is nothing to offer', () => {
    expect(invitableMeetingOptions(null, undefined)).toEqual([]);
  });
});
