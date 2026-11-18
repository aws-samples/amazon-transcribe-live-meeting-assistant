/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import React from 'react';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import { describe, it, expect, beforeEach, vi } from 'vitest';

import RejoinVirtualParticipant, { canRejoin } from './RejoinVirtualParticipant';

const { graphqlMock } = vi.hoisted(() => ({ graphqlMock: vi.fn() }));
vi.mock('aws-amplify/api', () => ({ generateClient: () => ({ graphql: graphqlMock }) }));
vi.mock('aws-amplify/utils', () => ({
  ConsoleLogger: class {
    // eslint-disable-next-line class-methods-use-this
    error() {}
  },
}));

const left = { id: 'vp-1', status: 'ENDED', CallId: 'call-1', meetingName: 'Weekly sync' };

const openAndConfirm = async () => {
  fireEvent.click(screen.getByRole('button', { name: 'Rejoin' }));
  fireEvent.click(await screen.findByRole('button', { name: 'Rejoin meeting' }));
};

describe('canRejoin', () => {
  it('offers Rejoin only for a Virtual Participant that has left a meeting it started', () => {
    ['ENDED', 'COMPLETED', 'FAILED'].forEach((status) => {
      expect(canRejoin({ ...left, status }, true)).toBe(true);
    });
    ['ACTIVE', 'JOINING', 'INITIALIZING', 'SCHEDULED', 'CANCELLED'].forEach((status) => {
      expect(canRejoin({ ...left, status }, true)).toBe(false);
    });
    expect(canRejoin({ ...left, CallId: null }, true)).toBe(false);
  });

  it('offers Rejoin only to a viewer who may manage the Virtual Participant', () => {
    expect(canRejoin(left, false)).toBe(false);
    expect(canRejoin(null, true)).toBe(false);
  });
});

describe('RejoinVirtualParticipant', () => {
  beforeEach(() => {
    graphqlMock.mockReset();
  });

  it('renders nothing where Rejoin is not offered', () => {
    const { container } = render(<RejoinVirtualParticipant vp={{ ...left, status: 'ACTIVE' }} canManage />);
    expect(container).toBeEmptyDOMElement();
  });

  it('rejoins after confirmation and hands the updated row back', async () => {
    const updated = { id: 'vp-1', status: 'INITIALIZING', CallId: 'call-1', sessionNumber: 2 };
    graphqlMock.mockResolvedValue({ data: { rejoinVirtualParticipant: updated } });
    const onRejoined = vi.fn();

    render(<RejoinVirtualParticipant vp={left} canManage onRejoined={onRejoined} />);
    expect(graphqlMock).not.toHaveBeenCalled();
    await openAndConfirm();

    await waitFor(() => expect(onRejoined).toHaveBeenCalledWith(updated));
    expect(graphqlMock).toHaveBeenCalledTimes(1);
    const [{ query, variables }] = graphqlMock.mock.calls[0];
    expect(query).toContain('rejoinVirtualParticipant(input: $input)');
    expect(query).toContain('Owner');
    expect(query).toContain('SharedWith');
    expect(variables).toEqual({ input: { id: 'vp-1' } });
  });

  it('shows why the server refused, and keeps the dialog open', async () => {
    graphqlMock.mockRejectedValue({
      errors: [{ message: "The meeting's transcript has been deleted, so it cannot be reopened." }],
    });
    const onRejoined = vi.fn();

    render(<RejoinVirtualParticipant vp={left} canManage onRejoined={onRejoined} />);
    await openAndConfirm();

    expect(await screen.findByText(/transcript has been deleted/)).toBeVisible();
    expect(screen.getByRole('button', { name: 'Rejoin meeting' })).toBeVisible();
    expect(onRejoined).not.toHaveBeenCalled();
  });
});
