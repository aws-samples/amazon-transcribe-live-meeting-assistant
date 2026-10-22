/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The configuration pages edit records that apply to every meeting in the
 * account, so they belong to the Admin group and the API enforces that on its
 * own. This guard is what makes the rule legible in the UI: an Admin gets the
 * form, anyone else gets an explanation rather than a save that is refused.
 *
 * Group membership is read from the session asynchronously, so the third case
 * below -- "the answer is not in yet" -- matters as much as the other two: acting
 * on the placeholder would flash the denial at an Admin on every page load.
 */
import React from 'react';
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';

// vi.mock factories are hoisted above the module scope, so anything they close
// over has to be hoisted with them.
const { userGroups } = vi.hoisted(() => ({
  userGroups: { isAdmin: false, userGroups: [], isLoading: false },
}));

vi.mock('aws-amplify/utils', () => ({
  ConsoleLogger: class {
    /* eslint-disable class-methods-use-this */
    debug() {}

    info() {}

    warn() {}

    error() {}
    /* eslint-enable class-methods-use-this */
  },
}));

vi.mock('../../hooks/use-user-groups', () => ({ default: () => userGroups }));

// eslint-disable-next-line import/first
import AdminOnly from './AdminOnly';

const renderGuard = ({ isAdmin, isLoading }) => {
  userGroups.isAdmin = isAdmin;
  userGroups.userGroups = isAdmin ? ['Admin'] : ['Users'];
  userGroups.isLoading = isLoading;
  return render(
    <AdminOnly feature="the test settings">
      <div>the configuration form</div>
    </AdminOnly>,
  );
};

describe('AdminOnly', () => {
  it('renders its children for a member of the Admin group', () => {
    renderGuard({ isAdmin: true, isLoading: false });

    expect(screen.getByText('the configuration form')).toBeTruthy();
    expect(screen.queryByText('Access denied')).toBeNull();
  });

  it('explains the restriction to a user outside the Admin group', () => {
    renderGuard({ isAdmin: false, isLoading: false });

    expect(screen.getByText('Access denied')).toBeTruthy();
    expect(screen.queryByText('the configuration form')).toBeNull();
  });

  it('names the setting an administrator would have to change', () => {
    renderGuard({ isAdmin: false, isLoading: false });

    expect(screen.getByText(/the test settings/)).toBeTruthy();
  });

  it('shows neither the form nor the explanation while group membership is unresolved', () => {
    renderGuard({ isAdmin: false, isLoading: true });

    expect(screen.queryByText('Access denied')).toBeNull();
    expect(screen.queryByText('the configuration form')).toBeNull();
    expect(screen.getByText(/Loading/)).toBeTruthy();
  });
});
