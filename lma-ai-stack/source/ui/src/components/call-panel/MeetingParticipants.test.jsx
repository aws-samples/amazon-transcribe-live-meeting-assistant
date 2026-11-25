/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import React from 'react';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { describe, it, expect, beforeEach, vi } from 'vitest';

import MeetingParticipants from './MeetingParticipants';

const { graphqlMock } = vi.hoisted(() => ({ graphqlMock: vi.fn() }));
vi.mock('aws-amplify/api', () => ({ generateClient: () => ({ graphql: graphqlMock }) }));
vi.mock('aws-amplify/utils', () => ({
  ConsoleLogger: class {
    error() {} // eslint-disable-line class-methods-use-this
  },
}));

const PEOPLE = [
  { name: 'Ana Lima', email: 'ana@example.com' },
  { name: '', email: 'bo@example.org' },
];

const answerWith = (accounts, created = null) => {
  graphqlMock.mockReset();
  graphqlMock.mockImplementation((request) => {
    const query = request?.query || '';
    if (query.includes('getMeetingParticipantAccounts(')) {
      return Promise.resolve({ data: { getMeetingParticipantAccounts: accounts() } });
    }
    if (query.includes('createParticipantAccounts(')) {
      return Promise.resolve({ data: { createParticipantAccounts: created } });
    }
    return Promise.reject(new Error(`unexpected ${query}`));
  });
};

const calls = (name) => graphqlMock.mock.calls.map(([c]) => c).filter((c) => c.query.includes(`${name}(`));

describe('MeetingParticipants', () => {
  beforeEach(() => {
    graphqlMock.mockReset();
  });

  it('shows the owner each person with their LMA account', async () => {
    answerWith(() => [
      { name: 'Ana Lima', email: 'ana@example.com', account: 'Participant' },
      { name: '', email: 'bo@example.org', account: 'none' },
    ]);
    render(<MeetingParticipants participants={PEOPLE} callId="c1" canManage />);
    expect(await screen.findByText('No account')).toBeTruthy();
    expect(screen.getByText('Participant')).toBeTruthy();
    expect(screen.getByText('LMA account')).toBeTruthy();
    expect(calls('getMeetingParticipantAccounts')[0].variables).toEqual({ CallId: 'c1' });
    const button = screen.getByText('Create accounts for participants without one').closest('button');
    expect(button.disabled || button.getAttribute('aria-disabled') === 'true').toBe(false);
  });

  it('shows a viewer the meeting is shared with the list only, and reads nothing', () => {
    render(<MeetingParticipants participants={PEOPLE} callId="c1" />);
    expect(screen.getByText('ana@example.com')).toBeTruthy();
    expect(screen.queryByText('LMA account')).toBeNull();
    expect(screen.queryByText('Create accounts for participants without one')).toBeNull();
    expect(screen.queryByText('Edit')).toBeNull();
    expect(graphqlMock).not.toHaveBeenCalled();
  });

  it('is hidden when the list is empty and the viewer cannot change it', () => {
    const { container } = render(<MeetingParticipants participants={[]} callId="c1" canManage />);
    expect(container.textContent).toBe('');
  });

  it('disables the button when everyone has an account', async () => {
    answerWith(() => PEOPLE.map((p) => ({ ...p, account: 'User' })));
    render(<MeetingParticipants participants={PEOPLE} callId="c1" canManage />);
    await waitFor(() => expect(screen.getAllByText('User')).toHaveLength(2));
    // A button with a disabled reason is aria-disabled, so its reason can be read.
    const button = screen.getByText('Create accounts for participants without one').closest('button');
    expect(button.disabled || button.getAttribute('aria-disabled') === 'true').toBe(true);
  });

  it('creates the accounts after a confirmation, then shows the outcome and reads the accounts again', async () => {
    let created = false;
    answerWith(
      () => [
        { name: 'Ana Lima', email: 'ana@example.com', account: 'Admin' },
        { name: '', email: 'bo@example.org', account: created ? 'Participant' : 'none' },
      ],
      { results: [{ email: 'bo@example.org', outcome: 'created', message: null }], created: 1, remainingToday: 24 },
    );
    render(<MeetingParticipants participants={PEOPLE} virtualParticipantId="vp-1" canManage />);
    fireEvent.click(await screen.findByText('Create accounts for participants without one'));
    const dialog = screen.getByText(/Participant role will be created for 1 person/);
    expect(dialog.textContent).toContain('bo@example.org');
    expect(calls('createParticipantAccounts')).toHaveLength(0);
    created = true;
    fireEvent.click(screen.getByText('Create accounts'));
    expect(await screen.findByText(/Created 1 Participant account/)).toBeTruthy();
    expect(screen.getByText(/24 more today/)).toBeTruthy();
    // Only the person without an account is sent, and the answer updates the
    // status without reading the list again.
    expect(calls('createParticipantAccounts')[0].variables).toEqual({
      input: { VirtualParticipantId: 'vp-1', emails: ['bo@example.org'] },
    });
    await waitFor(() => expect(screen.queryByText('No account')).toBeNull());
    expect(calls('getMeetingParticipantAccounts')).toHaveLength(1);
  });

  it('saves an edited list through onSave and refuses an invalid entry', async () => {
    answerWith(() => []);
    const onSave = vi.fn(() => Promise.resolve({ ok: true }));
    render(<MeetingParticipants participants={PEOPLE} callId="c1" canManage onSave={onSave} />);
    fireEvent.click(screen.getByText('Edit'));
    const field = screen.getByLabelText('Participants, one Name <email> per line');
    expect(field.value).toBe('Ana Lima <ana@example.com>\nbo@example.org');
    fireEvent.change(field, { target: { value: 'nobody' } });
    const save = () => within(screen.getByTestId('meeting-participants')).getByText('Save').closest('button');
    expect(save().disabled).toBe(true);
    fireEvent.change(field, { target: { value: 'Cy <cy@example.net>' } });
    fireEvent.click(save());
    await waitFor(() => expect(onSave).toHaveBeenCalledWith([{ name: 'Cy', email: 'cy@example.net' }]));
  });

  it('lets the owner add a list to a meeting that has none', () => {
    render(<MeetingParticipants participants={[]} callId="c1" canManage onSave={vi.fn()} />);
    expect(screen.getByText('No participants listed.')).toBeTruthy();
    expect(screen.getByText('Edit')).toBeTruthy();
  });
});
