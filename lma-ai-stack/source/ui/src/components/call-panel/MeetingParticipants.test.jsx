/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import React from 'react';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { describe, it, expect, beforeEach, vi } from 'vitest';

import createWrapper from '@cloudscape-design/components/test-utils/dom';

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

  it('creates the accounts after a confirmation and shows the outcome and the allowance left today', async () => {
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

  it('lets the owner add a list to a meeting that has none, from the header and from the empty list', async () => {
    const onSave = vi.fn(() => Promise.resolve({ ok: true }));
    render(<MeetingParticipants participants={[]} callId="c1" canManage onSave={onSave} />);
    expect(screen.getByText('No participants listed.')).toBeTruthy();
    expect(screen.queryByText('Edit')).toBeNull();
    expect(screen.getByText(/can be changed at any time, before, during and after the meeting/)).toBeTruthy();
    // One in the header, one in the empty list.
    expect(screen.getAllByText('Add participants')).toHaveLength(2);
    fireEvent.click(screen.getByLabelText('Add participants to the list'));
    const field = screen.getByLabelText('Participants, one Name <email> per line');
    expect(field.value).toBe('');
    fireEvent.change(field, { target: { value: 'Cy <cy@example.net>' } });
    fireEvent.click(within(screen.getByTestId('meeting-participants')).getByText('Save').closest('button'));
    await waitFor(() => expect(onSave).toHaveBeenCalledWith([{ name: 'Cy', email: 'cy@example.net' }]));
  });

  it('labels the button Edit once the list has people on it', () => {
    answerWith(() => []);
    render(<MeetingParticipants participants={PEOPLE} callId="c1" canManage onSave={vi.fn()} />);
    expect(screen.getByText('Edit')).toBeTruthy();
    expect(screen.queryByText('Add participants')).toBeNull();
  });

  it('shows the message the save answers with and stays in the editor', async () => {
    answerWith(() => []);
    const onSave = vi.fn(() => Promise.resolve({ ok: false, message: 'Unauthorized' }));
    render(<MeetingParticipants participants={PEOPLE} callId="c1" canManage onSave={onSave} />);
    fireEvent.click(screen.getByText('Edit'));
    fireEvent.click(within(screen.getByTestId('meeting-participants')).getByText('Save').closest('button'));
    expect(await screen.findByText('Unauthorized')).toBeTruthy();
    expect(screen.getByLabelText('Participants, one Name <email> per line')).toBeTruthy();
  });
});

describe('the invitation to consent', () => {
  beforeEach(() => {
    answerWith(() => PEOPLE.map((p) => ({ ...p, account: 'Participant' })));
  });

  it('is offered to the meeting owner only, not to an Admin who does not own it', async () => {
    const { unmount } = render(<MeetingParticipants participants={PEOPLE} callId="c1" canManage />);
    await screen.findAllByText('Participant');
    expect(screen.queryByText('Invite participants to consent')).toBeNull();
    unmount();
    render(<MeetingParticipants participants={PEOPLE} callId="c1" canManage isOwner />);
    expect(await screen.findByText('Invite participants to consent')).toBeTruthy();
  });

  it('is offered once the list has people on it, and not while it is empty', async () => {
    const { rerender } = render(
      <MeetingParticipants participants={[]} callId="c1" canManage isOwner onSave={vi.fn()} />,
    );
    expect(screen.queryByText('Invite participants to consent')).toBeNull();
    rerender(<MeetingParticipants participants={PEOPLE} callId="c1" canManage isOwner onSave={vi.fn()} />);
    expect(await screen.findByText('Invite participants to consent')).toBeTruthy();
  });

  it('builds one email to everyone listed with one link to the meeting’s consent page', async () => {
    render(
      <MeetingParticipants
        participants={PEOPLE}
        virtualParticipantId="vp-1"
        meetingName="Weekly sync"
        owner="owner@example.com"
        canManage
        isOwner
      />,
    );
    fireEvent.click(await screen.findByText('Invite participants to consent'));
    const dialog = await screen.findByRole('dialog', { name: 'Invite participants to consent' });
    expect(within(dialog).getByText('ana@example.com, bo@example.org')).toBeTruthy();
    expect(within(dialog).getByText('Consent to recording: Weekly sync')).toBeTruthy();
    const text = within(dialog).getByLabelText('Invitation text').value;
    expect(text).toMatch(/\/#\/consents\?vp=vp-1\n/);
    const mail = within(dialog).getByText('Open in mail app').closest('a');
    expect(mail.getAttribute('href')).toMatch(/^mailto:ana%40example\.com,bo%40example\.org\?subject=/);
    // Nothing is sent by LMA.
    expect(calls('createParticipantAccounts')).toHaveLength(0);
  });

  it('shows the owner who asked to be added', async () => {
    render(
      <MeetingParticipants
        participants={PEOPLE}
        virtualParticipantId="vp-1"
        canManage
        requests={[
          { name: 'Cy Diaz', email: 'cy@example.net', requestedAt: '2026-11-25T10:00:00Z' },
          // Added to the list since: no longer shown.
          { name: 'Bo', email: 'BO@example.org', requestedAt: '2026-11-25T10:00:00Z' },
        ]}
      />,
    );
    const alert = await screen.findByText(/Cy Diaz <cy@example.net>/);
    expect(alert.textContent).not.toContain('BO@example.org');
    expect(alert.textContent).toMatch(/^This person/);
  });
});

describe('the account picker in the list editor', () => {
  const answerSearch = (found) => {
    graphqlMock.mockReset();
    graphqlMock.mockImplementation((request) => {
      const query = request?.query || '';
      if (query.includes('getMeetingParticipantAccounts(')) {
        return Promise.resolve({ data: { getMeetingParticipantAccounts: [] } });
      }
      if (query.includes('searchAccounts(')) return Promise.resolve({ data: { searchAccounts: found } });
      return Promise.reject(new Error(`unexpected ${query}`));
    });
  };

  const openEditor = () => {
    render(<MeetingParticipants participants={PEOPLE} callId="c1" canManage onSave={vi.fn()} />);
    fireEvent.click(screen.getByText('Edit'));
    return screen.getByLabelText('Find an LMA account to add');
  };

  it('searches only once three characters are typed', async () => {
    answerSearch([]);
    const input = openEditor();
    fireEvent.focus(input);
    fireEvent.change(input, { target: { value: 'ro' } });
    await new Promise((resolve) => {
      setTimeout(resolve, 400);
    });
    expect(calls('searchAccounts')).toHaveLength(0);
    fireEvent.change(input, { target: { value: 'rob' } });
    await waitFor(() => expect(calls('searchAccounts')).toHaveLength(1));
    expect(calls('searchAccounts')[0].variables).toEqual({ text: 'rob' });
  });

  it('adds the account picked to the list as Name <email>', async () => {
    answerSearch([{ name: 'Robert Chen', email: 'robert@example.com' }]);
    openEditor();
    const picker = createWrapper(screen.getByTestId('meeting-participants')).findAutosuggest();
    picker.focus();
    picker.setInputValue('rob');
    await waitFor(() => expect(picker.findDropdown().findOptionByValue('robert@example.com')).toBeTruthy());
    picker.selectSuggestionByValue('robert@example.com');
    const field = screen.getByLabelText('Participants, one Name <email> per line');
    await waitFor(() =>
      expect(field.value).toBe('Ana Lima <ana@example.com>\nbo@example.org\nRobert Chen <robert@example.com>'),
    );
  });

  it('is not offered to someone who can only view the list', () => {
    render(<MeetingParticipants participants={PEOPLE} callId="c1" />);
    expect(screen.queryByLabelText('Find an LMA account to add')).toBeNull();
  });
});
