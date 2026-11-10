/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React from 'react';
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';

import { mergeVpUpdate, vpOwner } from './vp-details';
import { ConnectionDetails } from './VirtualParticipantDetails';

// VirtualParticipantDetails.jsx calls generateClient() / ConsoleLogger at module load.
vi.mock('aws-amplify/api', () => ({ generateClient: () => ({ graphql: vi.fn() }) }));
vi.mock('aws-amplify/utils', () => ({ ConsoleLogger: class {} }));

const CREATED = {
  id: 'vp-1',
  status: 'INITIALIZING',
  createdAt: '2026-11-09T10:00:00Z',
  updatedAt: '2026-11-09T10:00:00Z',
  meetingPlatform: 'ZOOM',
  meetingId: '123',
  owner: 'alex@example.com',
  Owner: 'alex@example.com',
  vncReady: false,
};

// What the Virtual Participant's own status write delivers through the
// subscription: the fields it selects, and null for the rest.
const STATUS_WRITE = {
  id: 'vp-1',
  status: 'JOINING',
  updatedAt: '2026-11-09T10:01:00Z',
  meetingName: 'Weekly sync',
  Owner: 'alex@example.com',
  owner: null,
  CallId: null,
  vncEndpoint: null,
  vncPort: null,
  vncReady: null,
  manualActionType: null,
  errorMessage: null,
};

describe('vpOwner', () => {
  it('reads Owner, the field written when the Virtual Participant is created', () => {
    expect(vpOwner({ Owner: 'alex@example.com' })).toBe('alex@example.com');
  });

  it('falls back to the lowercase owner, and to nothing', () => {
    expect(vpOwner({ owner: 'sam@example.com' })).toBe('sam@example.com');
    expect(vpOwner({})).toBe('');
    expect(vpOwner(null)).toBe('');
  });
});

describe('mergeVpUpdate', () => {
  it('keeps the owner when a status update does not carry the lowercase field', () => {
    const next = mergeVpUpdate(CREATED, STATUS_WRITE);
    expect(next.owner).toBe('alex@example.com');
    expect(next.Owner).toBe('alex@example.com');
    expect(next.status).toBe('JOINING');
    expect(next.updatedAt).toBe('2026-11-09T10:01:00Z');
    expect(next.meetingName).toBe('Weekly sync');
    expect(next.createdAt).toBe(CREATED.createdAt);
  });

  it('keeps the status and the viewer fields an update leaves out', () => {
    const prev = { ...CREATED, CallId: 'call-1', vncEndpoint: 'wss://x', vncPort: 5901, vncReady: true };
    const next = mergeVpUpdate(prev, { id: 'vp-1', status: null, vncReady: false, CallId: null });
    expect(next.status).toBe('INITIALIZING');
    expect(next.CallId).toBe('call-1');
    expect(next.vncEndpoint).toBe('wss://x');
    expect(next.vncReady).toBe(true);
  });

  it('takes the values an update does carry, including a cleared error message', () => {
    const prev = { ...CREATED, errorMessage: 'Retrying' };
    const next = mergeVpUpdate(prev, { id: 'vp-1', status: 'FAILED', errorMessage: null, vncReady: true });
    expect(next.status).toBe('FAILED');
    expect(next.errorMessage).toBeNull();
    expect(next.vncReady).toBe(true);
  });
});

describe('the Owner field on the detail page', () => {
  it('shows the owner while the Virtual Participant is still starting', () => {
    render(<ConnectionDetails vpDetails={mergeVpUpdate(CREATED, STATUS_WRITE)} />);
    expect(screen.getByText('alex@example.com')).toBeTruthy();
    expect(screen.queryByText('N/A')).toBeNull();
  });

  it('shows the owner from Owner when the lowercase field is absent', () => {
    render(<ConnectionDetails vpDetails={{ ...CREATED, owner: null }} />);
    expect(screen.getByText('alex@example.com')).toBeTruthy();
  });
});
