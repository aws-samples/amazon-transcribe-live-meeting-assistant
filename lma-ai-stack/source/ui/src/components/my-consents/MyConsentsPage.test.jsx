/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * My consents and a meeting's consent page.
 *
 * What a stored consent attests to depends on the page: the person read the
 * exact text it keeps (the hash sent is the one the server returned with that
 * text), took an explicit step to agree, and someone not on the list is told
 * so and can only ask to be added.
 */
import React from 'react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

const { graphql } = vi.hoisted(() => ({ graphql: vi.fn() }));

vi.mock('aws-amplify/api', () => ({ generateClient: () => ({ graphql }) }));
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

// eslint-disable-next-line import/first
import MyConsentsPage from './MyConsentsPage';

const TEXT = 'I agree that owner@example.com may record and transcribe the meeting "Weekly sync".';

const consent = (overrides = {}) => ({
  ConsentId: '20261125120000-aaaaaaaaaaaa',
  Scope: 'meeting',
  CallId: 'Weekly sync',
  MeetingName: 'Weekly sync',
  Organizer: 'owner@example.com',
  GrantedAt: '2026-11-25T12:00:00Z',
  ValidUntil: '2027-11-25T12:00:00Z',
  WithdrawnAt: null,
  WithdrawnReason: null,
  NoticeText: TEXT,
  NoticeHash: 'a'.repeat(64),
  NoticeVersion: 'abcdefabcdef',
  Source: 'pre_meeting',
  Email: 'ana@example.com',
  Status: 'active',
  DeviceDetailsKept: false,
  ...overrides,
});

const listed = (overrides = {}) => ({
  Listed: true,
  Requested: false,
  Enabled: true,
  Email: 'ana@example.com',
  ListedName: 'Ana Silva',
  CallId: 'Weekly sync',
  VirtualParticipantId: null,
  MeetingName: 'Weekly sync',
  Organizer: 'owner@example.com',
  ScheduledFor: null,
  Labels: [{ Key: 'engagement', Value: 'acme' }],
  Scope: 'meeting',
  ValidUntil: '2027-11-25T12:00:00Z',
  NoticeText: TEXT,
  NoticeHash: 'b'.repeat(64),
  MaxDays: 365,
  ActiveConsents: [],
  ...overrides,
});

/** Answers each operation by name, so the order the page calls them in does not matter. */
const answer = (handlers) => {
  graphql.mockImplementation(({ query, variables }) => {
    const name = Object.keys(handlers).find((op) => query.includes(`${op}(`) || query.includes(`${op} {`));
    if (!name) throw new Error(`unexpected operation ${query.slice(0, 80)}`);
    return Promise.resolve({ data: { [name]: handlers[name](variables) } });
  });
};

const renderAt = (path) =>
  render(
    <MemoryRouter initialEntries={[path]}>
      <MyConsentsPage />
    </MemoryRouter>,
  );

const calls = (name) => graphql.mock.calls.filter(([{ query }]) => query.includes(`${name}(`)).map(([c]) => c);

beforeEach(() => vi.clearAllMocks());
afterEach(cleanup);

describe('My consents', () => {
  it('lists the person’s consents with what they cover and the text agreed to', async () => {
    answer({
      listMyAdvanceConsents: () => ({
        Enabled: true,
        MaxDays: 365,
        Email: 'ana@example.com',
        Consents: [
          consent(),
          consent({
            ConsentId: '2',
            Scope: 'organizer',
            CallId: null,
            Status: 'withdrawn',
            WithdrawnReason: 'withdrawn',
          }),
        ],
      }),
    });
    renderAt('/consents');
    expect(await screen.findByText('This meeting: Weekly sync')).toBeTruthy();
    expect(screen.getByText('Meetings organized by owner@example.com')).toBeTruthy();
    expect(screen.getByText('Kept under ana@example.com.')).toBeTruthy();
    expect(screen.getAllByText('In force')).toHaveLength(1);
    expect(screen.getAllByText(TEXT).length).toBeGreaterThan(0);
    // Only the consent in force can be withdrawn.
    expect(screen.getAllByRole('button', { name: /^Withdraw .+/ })).toHaveLength(1);
  });

  it('withdraws a consent after confirming, and reads the list again', async () => {
    let withdrawn = false;
    answer({
      listMyAdvanceConsents: () => ({
        Email: 'ana@example.com',
        Consents: [consent(withdrawn ? { Status: 'withdrawn', WithdrawnAt: '2026-11-26T00:00:00Z' } : {})],
      }),
      withdrawAdvanceConsent: () => {
        withdrawn = true;
        return consent({ Status: 'withdrawn' });
      },
    });
    renderAt('/consents');
    fireEvent.click(await screen.findByRole('button', { name: 'Withdraw This meeting: Weekly sync' }));
    const dialog = await screen.findByRole('dialog');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Withdraw' }));
    await waitFor(() => expect(calls('withdrawAdvanceConsent')).toHaveLength(1));
    expect(calls('withdrawAdvanceConsent')[0].variables).toEqual({
      input: { ConsentId: '20261125120000-aaaaaaaaaaaa' },
    });
    await waitFor(() => expect(screen.queryByText('In force')).toBeNull());
  });
});

describe('a meeting’s consent page', () => {
  it('shows the exact text and gives consent only after the person agrees, with that text’s hash', async () => {
    answer({
      listMyAdvanceConsents: () => ({ Email: 'ana@example.com', Consents: [] }),
      getAdvanceConsentInvitation: () => listed(),
      giveAdvanceConsent: () => consent(),
    });
    renderAt('/consents?meeting=Weekly%20sync');
    expect(await screen.findByText('Consent for Weekly sync')).toBeTruthy();
    expect(screen.getByTestId('advance-consent-text').textContent).toBe(TEXT);
    expect(calls('getAdvanceConsentInvitation')[0].variables).toEqual({ CallId: 'Weekly sync', Scope: 'meeting' });
    const give = screen.getByRole('button', { name: 'Give consent' });
    expect(give.disabled).toBe(true);
    fireEvent.click(screen.getByLabelText('I have read this text and agree to it'));
    fireEvent.click(give);
    await waitFor(() => expect(calls('giveAdvanceConsent')).toHaveLength(1));
    expect(calls('giveAdvanceConsent')[0].variables).toEqual({
      input: { CallId: 'Weekly sync', Scope: 'meeting', NoticeHash: 'b'.repeat(64) },
    });
    expect(await screen.findByText(/Your consent is recorded/)).toBeTruthy();
  });

  it('asks the server for the standing consent’s own text, with a date', async () => {
    answer({
      listMyAdvanceConsents: () => ({ Consents: [] }),
      getAdvanceConsentInvitation: (variables) =>
        listed(variables.Scope === 'organizer' ? { Scope: 'organizer', NoticeText: 'standing text' } : {}),
    });
    renderAt('/consents?vp=vp-1');
    fireEvent.click(await screen.findByLabelText(/Meetings organized by owner@example.com/));
    await waitFor(() => expect(screen.getByTestId('advance-consent-text').textContent).toBe('standing text'));
    const last = calls('getAdvanceConsentInvitation').at(-1).variables;
    expect(last.VirtualParticipantId).toBe('vp-1');
    expect(last.Scope).toBe('organizer');
    expect(last.ValidUntil).toMatch(/^\d{4}-\d{2}-\d{2}$/);
  });

  it('tells someone not on the list so, and lets them ask to be added', async () => {
    answer({
      listMyAdvanceConsents: () => ({ Consents: [] }),
      getAdvanceConsentInvitation: () => ({ Listed: false, Requested: false, Enabled: true, Email: 'bo@example.org' }),
      askToBeAddedToMeeting: () => ({ Requested: true, Listed: false }),
    });
    renderAt('/consents?meeting=Weekly%20sync');
    expect(await screen.findByText('You are not on this meeting’s list'.replace('’', "'"))).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Give consent' })).toBeNull();
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Bo Chen' } });
    fireEvent.click(screen.getByRole('button', { name: 'Ask to be added' }));
    await waitFor(() => expect(calls('askToBeAddedToMeeting')).toHaveLength(1));
    expect(calls('askToBeAddedToMeeting')[0].variables).toEqual({
      input: { CallId: 'Weekly sync', Name: 'Bo Chen' },
    });
    expect(await screen.findByText(/You asked to be added/)).toBeTruthy();
  });

  it('says when the person has already asked', async () => {
    answer({
      listMyAdvanceConsents: () => ({ Consents: [] }),
      getAdvanceConsentInvitation: () => ({ Listed: false, Requested: true, Enabled: true, Email: 'bo@example.org' }),
    });
    renderAt('/consents?meeting=Weekly%20sync');
    expect(await screen.findByText(/You asked to be added/)).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Ask to be added' })).toBeNull();
  });

  it('names the consents already in force for the meeting', async () => {
    answer({
      listMyAdvanceConsents: () => ({ Consents: [consent()] }),
      getAdvanceConsentInvitation: () => listed({ ActiveConsents: [consent()] }),
    });
    renderAt('/consents?meeting=Weekly%20sync');
    expect(await screen.findByText(/You already have consent in force for this meeting: This meeting/)).toBeTruthy();
  });
});
