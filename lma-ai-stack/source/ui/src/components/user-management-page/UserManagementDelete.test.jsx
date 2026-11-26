/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Deleting a user: their consent records are erased only when the Admin ticks
 * "Also permanently delete their consent records", which is off each time the
 * dialog opens.
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
  },
];

const deletes = () => graphqlMock.mock.calls.map(([c]) => c).filter((c) => c.query.includes('mutation DeleteUser'));

beforeEach(() => {
  graphqlMock.mockReset();
  graphqlMock.mockImplementation(({ query }) => {
    if (query.includes('query ListUsers')) {
      return Promise.resolve({ data: { listUsers: { users: USERS, lastSignInAvailable: true } } });
    }
    return Promise.resolve({ data: { deleteUser: { username: 'ana@example.com', success: true } } });
  });
});

const openDelete = async () => {
  render(<UserManagementPage />);
  const row = (await screen.findByText('ana@example.com')).closest('tr');
  fireEvent.click(within(row).getByRole('checkbox'));
  fireEvent.click(screen.getAllByRole('button', { name: 'Delete' })[0]);
  return screen.findByRole('dialog', { name: 'Delete user' });
};

describe('Delete user: consent records', () => {
  it('keeps the consent records unless the box is ticked', async () => {
    const dialog = await openDelete();
    expect(within(dialog).getByText(/withdrawn and kept for the meeting record retention period/)).toBeTruthy();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Delete' }));
    await waitFor(() => expect(deletes()).toHaveLength(1));
    expect(deletes()[0].variables).toEqual({ input: { username: 'ana@example.com' } });
  });

  it('asks for the consent records to be erased when the box is ticked', async () => {
    const dialog = await openDelete();
    fireEvent.click(within(dialog).getByText('Also permanently delete their consent records'));
    fireEvent.click(within(dialog).getByRole('button', { name: 'Delete' }));
    await waitFor(() => expect(deletes()).toHaveLength(1));
    expect(deletes()[0].variables).toEqual({
      input: { username: 'ana@example.com', deleteConsentRecords: true },
    });
  });
});
