/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The User Management page's Last sign-in and Last active columns: a recorded
 * time is shown, a user with none shows "Not recorded" (never a blank cell), and
 * when the server could not read the times every user shows "Unavailable". Role,
 * status, enabled and created are still shown.
 */
import React from 'react';
import { render, screen, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import UserManagementPage, {
  NOT_RECORDED_TEXT,
  UNAVAILABLE_TEXT,
  lastActiveText,
  lastSignInText,
} from './UserManagementPage';

const { graphqlMock, state } = vi.hoisted(() => ({ graphqlMock: vi.fn(), state: { listUsers: null } }));
vi.mock('aws-amplify/api', () => ({
  generateClient: () => ({ graphql: graphqlMock }),
}));

const SIGNED_IN_AT = '2026-11-20T09:30:00.000Z';
const ACTIVE_AT = '2026-11-24T16:45:00.000Z';

const users = [
  {
    username: 'admin@example.com',
    email: 'admin@example.com',
    role: 'Admin',
    status: 'CONFIRMED',
    enabled: true,
    createdAt: '2026-01-02T03:04:05.000Z',
    lastSignInAt: SIGNED_IN_AT,
    lastActiveAt: ACTIVE_AT,
  },
  {
    username: 'new@example.com',
    email: 'new@example.com',
    role: 'User',
    status: 'FORCE_CHANGE_PASSWORD',
    enabled: false,
    createdAt: '2026-11-23T12:00:00.000Z',
    lastSignInAt: null,
    lastActiveAt: null,
  },
];

beforeEach(() => {
  state.listUsers = { users, lastSignInAvailable: true };
  graphqlMock.mockReset();
  graphqlMock.mockImplementation(({ query }) => {
    if (query.includes('query ListUsers')) return Promise.resolve({ data: { listUsers: state.listUsers } });
    return Promise.resolve({ data: {} });
  });
});

const rowOf = async (email) => (await screen.findByText(email)).closest('tr');

describe('UserManagementPage last sign-in', () => {
  it('asks for each user’s last sign-in and whether the times were read', async () => {
    render(<UserManagementPage />);
    await screen.findByText('admin@example.com');
    const { query } = graphqlMock.mock.calls[0][0];
    expect(query).toContain('lastSignInAt');
    expect(query).toContain('lastSignInAvailable');
  });

  it('shows the column, a recorded time, and "Not recorded" for a user with none', async () => {
    render(<UserManagementPage />);
    expect(await screen.findByText('Last sign-in')).toBeInTheDocument();

    const admin = within(await rowOf('admin@example.com'));
    expect(admin.getByText(new Date(SIGNED_IN_AT).toLocaleString())).toBeInTheDocument();
    expect(admin.queryByText(NOT_RECORDED_TEXT)).not.toBeInTheDocument();

    const fresh = within(await rowOf('new@example.com'));
    // Neither a sign-in nor any activity recorded.
    expect(fresh.getAllByText(NOT_RECORDED_TEXT)).toHaveLength(2);
  });

  it('keeps showing role, status, enabled and created', async () => {
    render(<UserManagementPage />);
    const fresh = within(await rowOf('new@example.com'));
    expect(fresh.getByText('User')).toBeInTheDocument();
    expect(fresh.getByText('FORCE_CHANGE_PASSWORD')).toBeInTheDocument();
    expect(fresh.getByText('No')).toBeInTheDocument();
    expect(fresh.getByText(new Date('2026-11-23T12:00:00.000Z').toLocaleString())).toBeInTheDocument();
  });

  it('shows "Unavailable" for every user when the times could not be read', async () => {
    state.listUsers = {
      users: users.map((u) => ({ ...u, lastSignInAt: null, lastActiveAt: null })),
      lastSignInAvailable: false,
    };
    render(<UserManagementPage />);

    const rows = await Promise.all(['admin@example.com', 'new@example.com'].map(rowOf));
    rows.forEach((tr) => {
      const row = within(tr);
      expect(row.getAllByText(UNAVAILABLE_TEXT)).toHaveLength(2);
      expect(row.queryByText(NOT_RECORDED_TEXT)).not.toBeInTheDocument();
    });
  });
});

describe('UserManagementPage last active', () => {
  it('asks for each user’s last activity', async () => {
    render(<UserManagementPage />);
    await screen.findByText('admin@example.com');
    expect(graphqlMock.mock.calls[0][0].query).toContain('lastActiveAt');
  });

  it('shows the column and each user’s recorded activity', async () => {
    render(<UserManagementPage />);
    expect(await screen.findByText('Last active')).toBeInTheDocument();

    const admin = within(await rowOf('admin@example.com'));
    expect(admin.getByText(new Date(ACTIVE_AT).toLocaleString())).toBeInTheDocument();
  });

  it('shows a time for a user active only through an API key, and "Not recorded" for the sign-in', async () => {
    state.listUsers = {
      users: [{ ...users[1], lastActiveAt: ACTIVE_AT }],
      lastSignInAvailable: true,
    };
    render(<UserManagementPage />);

    const fresh = within(await rowOf('new@example.com'));
    expect(fresh.getByText(new Date(ACTIVE_AT).toLocaleString())).toBeInTheDocument();
    expect(fresh.getByText(NOT_RECORDED_TEXT)).toBeInTheDocument();
  });
});

describe('lastActiveText', () => {
  it('is never blank', () => {
    expect(lastActiveText({ lastActiveAt: null }, true)).toBe(NOT_RECORDED_TEXT);
    expect(lastActiveText({}, true)).toBe(NOT_RECORDED_TEXT);
    expect(lastActiveText({ lastActiveAt: ACTIVE_AT }, false)).toBe(UNAVAILABLE_TEXT);
    expect(lastActiveText({ lastActiveAt: ACTIVE_AT }, true)).toBe(new Date(ACTIVE_AT).toLocaleString());
  });
});

describe('lastSignInText', () => {
  it('is never blank', () => {
    expect(lastSignInText({ lastSignInAt: null }, true)).toBe(NOT_RECORDED_TEXT);
    expect(lastSignInText({}, true)).toBe(NOT_RECORDED_TEXT);
    expect(lastSignInText({ lastSignInAt: SIGNED_IN_AT }, false)).toBe(UNAVAILABLE_TEXT);
    expect(lastSignInText({ lastSignInAt: SIGNED_IN_AT }, true)).toBe(new Date(SIGNED_IN_AT).toLocaleString());
  });
});
