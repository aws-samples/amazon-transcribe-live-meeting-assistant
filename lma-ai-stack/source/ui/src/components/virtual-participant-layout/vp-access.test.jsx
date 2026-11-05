/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React from 'react';
import { MemoryRouter } from 'react-router-dom';
import { render, screen } from '@testing-library/react';
import { describe, it, expect, vi } from 'vitest';

import { canManageAll, canManageVirtualParticipant } from './vp-access';
import { ActionButtons } from './VirtualParticipantDetails';
import { VpDeleteModal } from './vp-table-config';

vi.mock('aws-amplify/api', () => ({ generateClient: () => ({ graphql: vi.fn() }) }));
vi.mock('aws-amplify/utils', () => ({ ConsoleLogger: class {} }));

const OWNER = { username: 'owner@example.com', isAdmin: false };
const ADMIN = { username: 'admin@example.com', isAdmin: true };
const OTHER = { username: 'bob@example.com', isAdmin: false };
const mine = { id: 'vp-1', Owner: 'owner@example.com', SharedWith: 'bob@example.com' };
const theirs = { id: 'vp-2', Owner: 'carol@example.com' };

describe('canManageVirtualParticipant', () => {
  it('allows the owner and Admins only', () => {
    expect(canManageVirtualParticipant(mine, OWNER)).toBe(true);
    expect(canManageVirtualParticipant(mine, ADMIN)).toBe(true);
    // Shared with is a read, not a change.
    expect(canManageVirtualParticipant(mine, OTHER)).toBe(false);
    expect(canManageVirtualParticipant(mine, {})).toBe(false);
  });

  it('allows a bulk delete only when every selection is allowed', () => {
    expect(canManageAll([mine], OWNER)).toBe(true);
    expect(canManageAll([mine, theirs], OWNER)).toBe(false);
    expect(canManageAll([mine, theirs], ADMIN)).toBe(true);
    expect(canManageAll([], ADMIN)).toBe(false);
  });
});

const renderActions = (vpDetails, canManage) =>
  render(
    <MemoryRouter>
      <ActionButtons
        vpDetails={vpDetails}
        onRefresh={() => {}}
        onEnd={() => {}}
        onCancelSchedule={() => {}}
        canManage={canManage}
      />
    </MemoryRouter>,
  );

describe('ActionButtons', () => {
  it('shows Cancel Schedule to a viewer who may manage the VP', () => {
    renderActions({ status: 'SCHEDULED', isScheduled: true }, true);
    expect(screen.getByText('Cancel Schedule')).toBeTruthy();
  });

  it('shows neither Cancel Schedule nor End to other viewers', () => {
    renderActions({ status: 'SCHEDULED', isScheduled: true }, false);
    expect(screen.queryByText('Cancel Schedule')).toBeNull();
    renderActions({ status: 'ACTIVE' }, false);
    expect(screen.queryByText('End Virtual Participant')).toBeNull();
  });

  it('shows End to a viewer who may manage an active VP', () => {
    renderActions({ status: 'ACTIVE' }, true);
    expect(screen.getByText('End Virtual Participant')).toBeTruthy();
  });
});

describe('VpDeleteModal', () => {
  const deleteButton = () => screen.getByRole('button', { name: 'Delete selected Virtual Participants' });

  it('enables delete for the owner', () => {
    render(<VpDeleteModal selectedItems={[mine]} viewer={OWNER} onRefresh={() => {}} onNotify={() => {}} />);
    expect(deleteButton().getAttribute('aria-disabled')).not.toBe('true');
  });

  it('disables delete when a selection belongs to someone else', () => {
    render(<VpDeleteModal selectedItems={[mine, theirs]} viewer={OWNER} onRefresh={() => {}} onNotify={() => {}} />);
    expect(deleteButton().getAttribute('aria-disabled')).toBe('true');
  });
});
