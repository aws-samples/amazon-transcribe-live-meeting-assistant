/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * My consents › All consents (admin): offered to Admins only, chosen in the
 * URL (?view=all), listed and filtered by the server, with a withdrawal on a
 * person's behalf, a deliberate permanent deletion, and a CSV export.
 */
import React from 'react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

const { graphql, groups } = vi.hoisted(() => ({ graphql: vi.fn(), groups: { value: [] } }));

vi.mock('aws-amplify/api', () => ({ generateClient: () => ({ graphql }) }));
vi.mock('aws-amplify/utils', () => ({
  ConsoleLogger: class {
    /* eslint-disable class-methods-use-this */
    debug() {}

    error() {}
    /* eslint-enable class-methods-use-this */
  },
}));
vi.mock('aws-amplify/auth', () => ({
  fetchAuthSession: () => Promise.resolve({ tokens: { idToken: { payload: { 'cognito:groups': groups.value } } } }),
}));
vi.mock('../../contexts/app', () => ({ default: () => null }));

/* eslint-disable import/first */
import MyConsentsPage from './MyConsentsPage';
import { consentsCsv, filterVariables } from './AllConsentsView';
/* eslint-enable import/first */

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
  NoticeText: 'I agree.',
  NoticeHash: 'a'.repeat(64),
  NoticeVersion: 'abcdefabcdef',
  Source: 'pre_meeting',
  Email: 'ana@example.com',
  Status: 'active',
  DisplayName: 'Ana Silva',
  CountedIn: [{ CallId: 'Weekly sync', At: '2026-11-25T13:00:00Z' }],
  Supersedes: [],
  ...overrides,
});

const answer = (handlers) => {
  graphql.mockImplementation(({ query, variables }) => {
    const name = Object.keys(handlers).find((op) => query.includes(`${op}(`) || query.includes(`${op} {`));
    if (!name) throw new Error(`unexpected operation ${query.slice(0, 80)}`);
    return Promise.resolve({ data: { [name]: handlers[name](variables) } });
  });
};

const calls = (name) => graphql.mock.calls.filter(([{ query }]) => query.includes(`${name}(`)).map(([c]) => c);

const renderAt = (path) =>
  render(
    <MemoryRouter initialEntries={[path]}>
      <MyConsentsPage />
    </MemoryRouter>,
  );

const HANDLERS = {
  listMyAdvanceConsents: () => ({ Enabled: true, MaxDays: 365, Email: 'admin@example.com', Consents: [] }),
  listAllAdvanceConsents: () => ({ Consents: [consent()], NextToken: null }),
  withdrawAdvanceConsentAsAdmin: () => consent({ Status: 'withdrawn' }),
  deleteAdvanceConsentRecords: () => ({ Deleted: 2, Withdrawn: 1, ConsentIds: ['x', 'y'], AuditId: 'audit-1' }),
};

beforeEach(() => {
  vi.clearAllMocks();
  groups.value = [];
});
afterEach(cleanup);

/** The delete-all confirmation's first sentence. */
const DELETE_ALL_TEXT = /^Every consent record kept under ana@example.com will be deleted, up to 1,000 at a time/;

const selectFirst = async () => {
  const row = (await screen.findByText('Ana Silva')).closest('tr');
  fireEvent.click(within(row).getByRole('radio'));
};

describe('All consents (admin)', () => {
  it('is not offered to someone who is not an Admin, even with ?view=all', async () => {
    answer(HANDLERS);
    renderAt('/consents?view=all');
    expect(await screen.findByRole('heading', { level: 1, name: 'My consents' })).toBeTruthy();
    expect(screen.queryByText('All consents (admin)')).toBeNull();
    expect(calls('listAllAdvanceConsents')).toHaveLength(0);
  });

  it('opens from ?view=all for an Admin, and lists everyone’s consents', async () => {
    groups.value = ['Admin'];
    answer(HANDLERS);
    renderAt('/consents?view=all');
    expect(await screen.findByRole('heading', { level: 1, name: 'All consents' })).toBeTruthy();
    expect(await screen.findByText('Ana Silva')).toBeTruthy();
    expect(screen.getByText('1 meeting')).toBeTruthy();
    expect(screen.getByText('My consents')).toBeTruthy();
  });

  it('reads on past empty answers until a page has consents', async () => {
    groups.value = ['Admin'];
    let reads = 0;
    answer({
      ...HANDLERS,
      listAllAdvanceConsents: () => {
        reads += 1;
        return reads < 3 ? { Consents: [], NextToken: `t${reads}` } : { Consents: [consent()], NextToken: null };
      },
    });
    renderAt('/consents?view=all');
    expect(await screen.findByText('Ana Silva')).toBeTruthy();
    expect(calls('listAllAdvanceConsents').map((c) => c.variables.NextToken)).toEqual([undefined, 't1', 't2']);
  });

  it('sends the filters to the server', async () => {
    groups.value = ['Admin'];
    answer(HANDLERS);
    renderAt('/consents?view=all');
    await screen.findByText('Ana Silva');
    fireEvent.change(screen.getByLabelText('Person'), { target: { value: ' ana@ ' } });
    fireEvent.click(screen.getByText('Apply filters', { selector: 'button *' }));
    await waitFor(() => expect(calls('listAllAdvanceConsents')).toHaveLength(2));
    expect(calls('listAllAdvanceConsents')[1].variables).toEqual({ Person: 'ana@', Limit: 50 });
  });

  it('withdraws a consent on the person’s behalf with a note', async () => {
    groups.value = ['Admin'];
    answer(HANDLERS);
    renderAt('/consents?view=all');
    await selectFirst();
    fireEvent.click(screen.getAllByRole('button', { name: 'Withdraw' })[0]);
    const dialog = await screen.findByRole('dialog', { name: "Withdraw consent on the person's behalf" });
    fireEvent.change(within(dialog).getByLabelText('Note to the person'), { target: { value: 'By phone' } });
    fireEvent.click(within(dialog).getByText('Withdraw', { selector: 'button *' }));
    await waitFor(() => expect(calls('withdrawAdvanceConsentAsAdmin')).toHaveLength(1));
    expect(calls('withdrawAdvanceConsentAsAdmin')[0].variables).toEqual({
      input: { Email: 'ana@example.com', ConsentId: '20261125120000-aaaaaaaaaaaa', Note: 'By phone' },
    });
  });

  it('deletes permanently only after the Admin confirms, naming what is deleted', async () => {
    groups.value = ['Admin'];
    answer(HANDLERS);
    renderAt('/consents?view=all');
    await selectFirst();
    fireEvent.click(screen.getByRole('button', { name: 'Delete all for this person' }));
    const dialog = await screen.findByRole('dialog', { name: 'Permanently delete consent records' });
    expect(within(dialog).getByText(DELETE_ALL_TEXT)).toBeTruthy();
    const button = within(dialog).getByText('Delete permanently', { selector: 'button *' }).closest('button');
    expect(button.disabled).toBe(true);
    fireEvent.click(within(dialog).getByText('I understand this cannot be undone.'));
    fireEvent.click(button);
    await waitFor(() => expect(calls('deleteAdvanceConsentRecords')).toHaveLength(1));
    expect(calls('deleteAdvanceConsentRecords')[0].variables).toEqual({
      input: { Email: 'ana@example.com', AllForPerson: true },
    });
    expect(
      await screen.findByText(/Deleted 2 consent records of ana@example.com, 1 of them withdrawn first/),
    ).toBeTruthy();
  });
});

describe('the export and the filters', () => {
  it('writes one CSV row per consent, quoting what needs it and defusing formulas', () => {
    const csv = consentsCsv([consent({ DisplayName: '=cmd, "x"' })]).split('\r\n');
    expect(csv[0].startsWith('Email,Display name,Consent id')).toBe(true);
    expect(csv[1]).toContain('"\'=cmd, ""x"""');
    expect(csv).toHaveLength(2);
  });

  it('sends only the filters that are set', () => {
    expect(filterVariables({ person: ' ', scope: '', state: 'active', expiringBefore: '2026-12-01' })).toEqual({
      State: 'active',
      ExpiringBefore: '2026-12-01',
    });
  });
});

describe('a read or an erasure that does not finish', () => {
  it('shows a failed listing with a way to try again, which reads again', async () => {
    groups.value = ['Admin'];
    let reads = 0;
    answer({
      ...HANDLERS,
      listAllAdvanceConsents: () => {
        reads += 1;
        if (reads === 1) throw new Error('The names of the people on this page could not be read; try again');
        return { Consents: [consent()], NextToken: null };
      },
    });
    renderAt('/consents?view=all');
    expect(await screen.findByText(/could not be read; try again/)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
    expect(await screen.findByText('Ana Silva')).toBeTruthy();
    expect(screen.queryByText(/could not be read; try again/)).toBeNull();
  });

  it('says when more records remain after an erasure, and deletes the next ones on request', async () => {
    groups.value = ['Admin'];
    let erasures = 0;
    answer({
      ...HANDLERS,
      deleteAdvanceConsentRecords: () => {
        erasures += 1;
        return erasures === 1
          ? { Deleted: 1000, Withdrawn: 0, ConsentIds: [], AuditId: 'audit-1', MoreRemain: true }
          : { Deleted: 5, Withdrawn: 0, ConsentIds: [], AuditId: 'audit-2', MoreRemain: false };
      },
    });
    renderAt('/consents?view=all');
    await selectFirst();
    fireEvent.click(screen.getByRole('button', { name: 'Delete all for this person' }));
    let dialog = await screen.findByRole('dialog', { name: 'Permanently delete consent records' });
    fireEvent.click(within(dialog).getByText('I understand this cannot be undone.'));
    fireEvent.click(within(dialog).getByText('Delete permanently', { selector: 'button *' }));
    expect(await screen.findByText(/More consent records remain under ana@example.com/)).toBeTruthy();
    expect(screen.getByText(/records this erasure as partial/)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Delete the next ones' }));
    dialog = await screen.findByRole('dialog', { name: 'Permanently delete consent records' });
    expect(within(dialog).getByText(DELETE_ALL_TEXT)).toBeTruthy();
    fireEvent.click(within(dialog).getByText('I understand this cannot be undone.'));
    fireEvent.click(within(dialog).getByText('Delete permanently', { selector: 'button *' }));
    expect(
      await screen.findByText(/Deleted 5 consent records of ana@example.com\. Audit entry audit-2\./),
    ).toBeTruthy();
    expect(calls('deleteAdvanceConsentRecords').map((c) => c.variables.input)).toEqual([
      { Email: 'ana@example.com', AllForPerson: true },
      { Email: 'ana@example.com', AllForPerson: true },
    ]);
  });
});
