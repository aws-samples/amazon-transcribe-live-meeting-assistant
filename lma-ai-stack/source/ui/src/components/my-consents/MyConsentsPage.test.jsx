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

const { graphql, appContext } = vi.hoisted(() => ({ graphql: vi.fn(), appContext: vi.fn(() => null) }));

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

vi.mock('../../contexts/app', () => ({ default: () => appContext() }));

/* eslint-disable import/first */
import MyConsentsPage from './MyConsentsPage';
import { CONSENT_PROMPT_TEXT } from './consent-prompt';
/* eslint-enable import/first */

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

describe('My consents with no link', () => {
  const standing = (variables) => ({
    Listed: false,
    Requested: false,
    Enabled: true,
    Email: 'bo@example.org',
    Scope: variables.Scope,
    Organizer: variables.Organizer || null,
    ValidUntil: `${variables.ValidUntil}T23:59:59Z`,
    LabelKey: variables.LabelKey || null,
    LabelValue: variables.LabelValue || null,
    NoticeText: `standing text for ${variables.Scope} ${variables.Organizer || ''}`.trim(),
    NoticeHash: 'c'.repeat(64),
    MaxDays: 180,
    ActiveConsents: [],
  });

  it('offers a standing consent for an organizer’s meetings or for every meeting, enabled', async () => {
    answer({ listMyAdvanceConsents: () => ({ Enabled: true, MaxDays: 180, Email: 'bo@example.org', Consents: [] }) });
    renderAt('/consents');
    expect(await screen.findByText('Give a standing consent')).toBeTruthy();
    const organizer = screen.getByLabelText(/Meetings organized by one person/);
    const everywhere = screen.getByLabelText(/All meetings on this deployment/);
    expect(organizer.disabled).toBe(false);
    expect(everywhere.disabled).toBe(false);
    // Nothing is chosen for the person, and nothing is asked of the server until they choose.
    expect(organizer.checked).toBe(false);
    expect(everywhere.checked).toBe(false);
    expect(calls('getAdvanceConsentInvitation')).toHaveLength(0);
    expect(screen.queryByRole('button', { name: 'Give consent' })).toBeNull();
  });

  it('gives a consent for every meeting with the text the server returned, after agreeing', async () => {
    answer({
      listMyAdvanceConsents: () => ({ Enabled: true, MaxDays: 180, Email: 'bo@example.org', Consents: [] }),
      getAdvanceConsentInvitation: standing,
      giveAdvanceConsent: () => consent({ Scope: 'deployment', CallId: null, Organizer: null }),
    });
    renderAt('/consents');
    fireEvent.click(await screen.findByLabelText(/All meetings on this deployment/));
    await waitFor(() =>
      expect(screen.getByTestId('standing-consent-text').textContent).toBe('standing text for deployment'),
    );
    const asked = calls('getAdvanceConsentInvitation').at(-1).variables;
    expect(asked.Scope).toBe('deployment');
    expect(asked.CallId).toBeUndefined();
    expect(asked.ValidUntil).toMatch(/^\d{4}-\d{2}-\d{2}$/);
    const give = screen.getByRole('button', { name: 'Give consent' });
    expect(give.disabled).toBe(true);
    fireEvent.click(screen.getByLabelText('I have read this text and agree to it'));
    fireEvent.click(give);
    await waitFor(() => expect(calls('giveAdvanceConsent')).toHaveLength(1));
    expect(calls('giveAdvanceConsent')[0].variables).toEqual({
      input: { Scope: 'deployment', ValidUntil: asked.ValidUntil, NoticeHash: 'c'.repeat(64) },
    });
    expect(await screen.findByText(/Your consent is recorded/)).toBeTruthy();
  });

  it('gives a consent for the organizer typed, optionally with a label', async () => {
    answer({
      listMyAdvanceConsents: () => ({ Enabled: true, MaxDays: 180, Consents: [] }),
      getAdvanceConsentInvitation: standing,
      giveAdvanceConsent: () => consent({ Scope: 'organizer', CallId: null }),
    });
    renderAt('/consents');
    fireEvent.click(await screen.findByLabelText(/Meetings organized by one person/));
    const address = screen.getByRole('combobox', { name: "Organizer's email address" });
    fireEvent.change(address, { target: { value: 'owner@exa' } });
    // Not yet an address: nothing is asked.
    expect(calls('getAdvanceConsentInvitation')).toHaveLength(0);
    fireEvent.change(address, { target: { value: 'owner@example.com' } });
    fireEvent.change(screen.getByRole('textbox', { name: 'Label name' }), { target: { value: 'engagement' } });
    fireEvent.change(screen.getByRole('textbox', { name: 'Label value' }), { target: { value: 'acme' } });
    await waitFor(() => expect(calls('getAdvanceConsentInvitation').at(-1).variables.LabelValue).toBe('acme'));
    await waitFor(() =>
      expect(screen.getByTestId('standing-consent-text').textContent).toBe(
        'standing text for organizer owner@example.com',
      ),
    );
    fireEvent.click(screen.getByLabelText('I have read this text and agree to it'));
    fireEvent.click(screen.getByRole('button', { name: 'Give consent' }));
    await waitFor(() => expect(calls('giveAdvanceConsent')).toHaveLength(1));
    expect(calls('giveAdvanceConsent')[0].variables.input).toMatchObject({
      Scope: 'organizer',
      Organizer: 'owner@example.com',
      LabelKey: 'engagement',
      LabelValue: 'acme',
      NoticeHash: 'c'.repeat(64),
    });
  });

  it('says when the deployment takes no consent given in advance', async () => {
    answer({ listMyAdvanceConsents: () => ({ Enabled: false, Consents: [] }) });
    renderAt('/consents');
    expect(await screen.findByText('This deployment does not take consent given in advance.')).toBeTruthy();
    expect(screen.queryByText('Give a standing consent')).toBeNull();
  });

  it('withdraws a consent for every meeting from the list', async () => {
    answer({
      listMyAdvanceConsents: () => ({
        Enabled: true,
        Consents: [consent({ Scope: 'deployment', CallId: null, Organizer: null, MeetingName: null })],
      }),
      withdrawAdvanceConsent: () => consent({ Scope: 'deployment', Status: 'withdrawn' }),
    });
    renderAt('/consents');
    fireEvent.click(await screen.findByRole('button', { name: 'Withdraw All meetings on this deployment' }));
    fireEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Withdraw' }));
    await waitFor(() => expect(calls('withdrawAdvanceConsent')).toHaveLength(1));
  });
});

describe('the prompt a Participant lands on', () => {
  const renderPrompt = (props = { offerPrompt: true }) =>
    render(
      <MemoryRouter initialEntries={['/consents']}>
        <MyConsentsPage offerPrompt={Boolean(props.offerPrompt)} />
      </MemoryRouter>,
    );

  beforeEach(() => {
    localStorage.clear();
    appContext.mockReturnValue({ currentSession: { tokens: { idToken: { payload: { sub: 'sub-bo' } } } } });
  });

  it('shows the banner to a person with no consent on record, and Not now only dismisses it', async () => {
    answer({ listMyAdvanceConsents: () => ({ Enabled: true, MaxDays: 365, Consents: [] }) });
    renderPrompt();
    expect(await screen.findByText(CONSENT_PROMPT_TEXT)).toBeTruthy();
    const before = graphql.mock.calls.length;
    fireEvent.click(screen.getByRole('button', { name: 'Not now' }));
    expect(screen.queryByText(CONSENT_PROMPT_TEXT)).toBeNull();
    expect(graphql.mock.calls.length).toBe(before);
    expect(localStorage.getItem('lma.consentPrompt.dismissed.sub-bo')).toBe('true');
    // The rest of the page is as it was.
    expect(screen.getByText('Give a standing consent')).toBeTruthy();
    cleanup();
    renderPrompt();
    await screen.findByText('Give a standing consent');
    expect(screen.queryByText(CONSENT_PROMPT_TEXT)).toBeNull();
  });

  it('is not shown with a consent on record, with it turned off, or outside a Participant’s pages', async () => {
    answer({ listMyAdvanceConsents: () => ({ Enabled: true, Consents: [consent()] }) });
    renderPrompt();
    await screen.findByText('This meeting: Weekly sync');
    expect(screen.queryByText(CONSENT_PROMPT_TEXT)).toBeNull();
    cleanup();
    answer({ listMyAdvanceConsents: () => ({ Enabled: false, Consents: [] }) });
    renderPrompt();
    await screen.findByText('This deployment does not take consent given in advance.');
    expect(screen.queryByText(CONSENT_PROMPT_TEXT)).toBeNull();
    cleanup();
    answer({ listMyAdvanceConsents: () => ({ Enabled: true, Consents: [] }) });
    renderPrompt({});
    await screen.findByText('Give a standing consent');
    expect(screen.queryByText(CONSENT_PROMPT_TEXT)).toBeNull();
  });
});
