/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import React from 'react';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, cleanup, waitFor, fireEvent } from '@testing-library/react';

const graphql = vi.fn();
vi.mock('aws-amplify/api', () => ({ generateClient: () => ({ graphql: (...args) => graphql(...args) }) }));

/* eslint-disable import/first */
import UserManagementPage, {
  LAST_ADMIN_REASON,
  OWN_ROLE_REASON,
  ROLE_OPTIONS,
  changeRoleBlockedReason,
} from './UserManagementPage';
import { AppContext } from '../../contexts/app';
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

  it('lists a Participant with that role, and offers Change role only for a selected user', async () => {
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

  describe('Change role availability', () => {
    const me = { username: 'me@example.com', email: 'me@example.com', role: 'Admin', enabled: true };
    const otherAdmin = { username: 'boss@example.com', email: 'boss@example.com', role: 'Admin', enabled: true };
    const plainUser = { username: 'pat@example.com', email: 'pat@example.com', role: 'User', enabled: true };

    const renderAs = async (users, username) => {
      graphql.mockResolvedValue({ data: { listUsers: { users } } });
      render(
        <AppContext.Provider value={{ user: { username } }}>
          <UserManagementPage />
        </AppContext.Provider>,
      );
      await waitFor(() => expect(screen.getByText(users[users.length - 1].email)).toBeTruthy());
    };

    const select = (email) => {
      const row = screen.getByText(email).closest('tr');
      fireEvent.click(row.querySelector('input[type="checkbox"]'));
    };

    const changeRoleButton = () => screen.getAllByRole('button', { name: 'Change role' })[0];
    // Cloudscape exposes disabledReason as the button's accessible description.
    const describedBy = (button) =>
      (button.getAttribute('aria-describedby') || '')
        .split(' ')
        .map((id) => document.getElementById(id)?.textContent || '')
        .join('')
        .trim();
    const isDisabled = (button) => button.disabled || button.getAttribute('aria-disabled') === 'true';

    it("disables Change role on the signed-in user's own row, with the reason", async () => {
      await renderAs([me, otherAdmin, plainUser], 'ME@example.com');
      select(me.email);
      const button = changeRoleButton();
      expect(isDisabled(button)).toBe(true);
      expect(describedBy(button)).toBe(OWN_ROLE_REASON);
    });

    it('disables Change role for the only remaining Admin, with the reason', async () => {
      await renderAs([otherAdmin, plainUser], 'someone-else@example.com');
      select(otherAdmin.email);
      const button = changeRoleButton();
      expect(isDisabled(button)).toBe(true);
      expect(describedBy(button)).toBe(LAST_ADMIN_REASON);
    });

    it('enables Change role for another user', async () => {
      await renderAs([me, otherAdmin, plainUser], me.username);
      select(plainUser.email);
      expect(isDisabled(changeRoleButton())).toBe(false);
    });

    it('computes the reason from the listed users', () => {
      expect(changeRoleBlockedReason(me, [me, plainUser], 'me@example.com')).toBe(OWN_ROLE_REASON);
      expect(changeRoleBlockedReason(otherAdmin, [otherAdmin, plainUser], 'me@example.com')).toBe(LAST_ADMIN_REASON);
      expect(changeRoleBlockedReason(otherAdmin, [me, otherAdmin], 'me@example.com')).toBeNull();
      expect(changeRoleBlockedReason(plainUser, [me, plainUser], 'me@example.com')).toBeNull();
      expect(changeRoleBlockedReason(null, [me], 'me@example.com')).toBeNull();
    });
  });
});
