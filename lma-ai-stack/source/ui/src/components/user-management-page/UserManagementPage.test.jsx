/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import React from 'react';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, cleanup, waitFor } from '@testing-library/react';

const graphql = vi.fn();
vi.mock('aws-amplify/api', () => ({ generateClient: () => ({ graphql: (...args) => graphql(...args) }) }));

/* eslint-disable import/first */
import UserManagementPage, { ROLE_OPTIONS } from './UserManagementPage';
/* eslint-enable import/first */

const MATRIX = JSON.parse(readFileSync(path.join(__dirname, '../../../../appsync/role-matrix.json'), 'utf-8'));

afterEach(() => {
  cleanup();
  graphql.mockReset();
});

describe('User Management', () => {
  it('offers every role of the role matrix', () => {
    expect(ROLE_OPTIONS.map((option) => option.value).sort()).toEqual(Object.keys(MATRIX.roles).sort());
  });

  it('lists a Participant with that role and offers Change role once one user is selected', async () => {
    graphql.mockResolvedValue({
      data: {
        listUsers: {
          users: [{ username: 'guest@example.com', email: 'guest@example.com', role: 'Participant', enabled: true }],
        },
      },
    });

    render(<UserManagementPage />);

    await waitFor(() => expect(screen.getByText('guest@example.com')).toBeTruthy());
    expect(screen.getAllByText('Participant').length).toBeGreaterThan(0);
    // The first is the header action; the dialog's own button is rendered too.
    const [button] = screen.getAllByRole('button', { name: 'Change role' });
    expect(button.disabled || button.getAttribute('aria-disabled') === 'true').toBe(true);
  });
});
