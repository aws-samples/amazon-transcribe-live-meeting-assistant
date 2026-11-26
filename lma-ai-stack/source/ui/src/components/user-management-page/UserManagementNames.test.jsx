/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The names a user goes by in meetings, on User Management: set when the user
 * is created, shown in the list, and changed for one selected user. createUser
 * is sent the names only when some are typed.
 */
import React from 'react';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import UserManagementPage from './UserManagementPage';

const { graphqlMock } = vi.hoisted(() => ({ graphqlMock: vi.fn() }));
vi.mock('aws-amplify/api', () => ({ generateClient: () => ({ graphql: graphqlMock }) }));

const USERS = [
  {
    username: 'ana@example.com',
    email: 'ana@example.com',
    role: 'Participant',
    status: 'CONFIRMED',
    enabled: true,
    createdAt: '2026-11-02T03:04:05.000Z',
    accountName: null,
    displayName: 'Ana Silva',
    shortNames: ['Ana'],
  },
  {
    username: 'bo@example.com',
    email: 'bo@example.com',
    role: 'User',
    status: 'CONFIRMED',
    enabled: true,
    createdAt: '2026-11-01T03:04:05.000Z',
    accountName: 'Bo Chen',
    displayName: null,
    shortNames: [],
  },
];

const calls = (name) => graphqlMock.mock.calls.map(([c]) => c).filter((c) => c.query.includes(name));

beforeEach(() => {
  graphqlMock.mockReset();
  graphqlMock.mockImplementation(({ query }) => {
    if (query.includes('query ListUsers')) {
      return Promise.resolve({ data: { listUsers: { users: USERS, lastSignInAvailable: true } } });
    }
    if (query.includes('query ListInvitableMeetings')) {
      return Promise.resolve({ data: { listVirtualParticipants: [], listCallsDateRange: { Calls: [] } } });
    }
    return Promise.resolve({ data: {} });
  });
});

describe('User Management: names in meetings', () => {
  it('shows each user’s display name and short names, or the account’s own name', async () => {
    render(<UserManagementPage />);
    expect(await screen.findByText('Ana Silva (Ana)')).toBeTruthy();
    expect(screen.getByText('Bo Chen')).toBeTruthy();
    expect(screen.getByText('Names in meetings')).toBeTruthy();
  });

  it('sends the names typed in Create user with the new user', async () => {
    render(<UserManagementPage />);
    fireEvent.click(await screen.findByText('Create user', { selector: 'button *' }));
    const dialog = await screen.findByRole('dialog', { name: 'Create user' });
    fireEvent.change(within(dialog).getByPlaceholderText('user@example.com'), { target: { value: 'cy@example.com' } });
    fireEvent.change(within(dialog).getByLabelText('Display name in meetings'), { target: { value: ' Cy  Diaz ' } });
    fireEvent.change(within(dialog).getByLabelText('Short names'), { target: { value: 'Cy, cy' } });
    fireEvent.click(within(dialog).getByText('Create', { selector: 'button *' }));
    await waitFor(() => expect(calls('mutation CreateUser')).toHaveLength(1));
    expect(calls('mutation CreateUser')[0].variables).toEqual({
      input: { email: 'cy@example.com', role: 'User', displayName: 'Cy Diaz', shortNames: ['Cy'] },
    });
  });

  it('refuses names the server would refuse before anything is sent', async () => {
    render(<UserManagementPage />);
    fireEvent.click(await screen.findByText('Create user', { selector: 'button *' }));
    const dialog = await screen.findByRole('dialog', { name: 'Create user' });
    fireEvent.change(within(dialog).getByPlaceholderText('user@example.com'), { target: { value: 'cy@example.com' } });
    fireEvent.change(within(dialog).getByLabelText('Short names'), { target: { value: 'a, b, c, d, e, f' } });
    fireEvent.click(within(dialog).getByText('Create', { selector: 'button *' }));
    expect((await within(dialog).findAllByText('At most 5 short names.')).length).toBeGreaterThan(0);
    expect(calls('mutation CreateUser')).toHaveLength(0);
  });

  it('changes the names of the one user selected', async () => {
    render(<UserManagementPage />);
    const row = (await screen.findByText('Ana Silva (Ana)')).closest('tr');
    fireEvent.click(within(row).getByRole('checkbox'));
    fireEvent.click(screen.getByText('Edit names', { selector: 'button *' }));
    const dialog = await screen.findByRole('dialog', { name: 'Edit names in meetings' });
    const display = within(dialog).getByLabelText('Display name in meetings');
    expect(display.value).toBe('Ana Silva');
    fireEvent.change(display, { target: { value: 'Ana Souza' } });
    fireEvent.click(within(dialog).getByText('Save', { selector: 'button *' }));
    await waitFor(() => expect(calls('mutation SetUserAccountNames')).toHaveLength(1));
    expect(calls('mutation SetUserAccountNames')[0].variables).toEqual({
      input: { username: 'ana@example.com', displayName: 'Ana Souza', shortNames: ['Ana'] },
    });
  });
});
