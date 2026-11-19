/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import React from 'react';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, it, expect, vi } from 'vitest';
import createWrapper from '@cloudscape-design/components/test-utils/dom';

import {
  JOIN_AGAIN_STATE_KEY,
  JOIN_MODE_CONTINUE,
  JOIN_MODE_NEW,
  MEETING_CHANGED_NOTE,
  NO_MEETING_REASON,
  SAVED_PASSWORD_HINT,
  SAVED_PASSWORD_NEW_HINT,
  TRANSCRIPT_DELETED_REASON,
  canJoinAgain,
  continueLabel,
  continueOptionFor,
  joinAgainFormChange,
  joinAgainFormFrom,
  joinAgainFromState,
  joinAgainInputFrom,
  joinAgainModeChange,
  joinAgainPasswordHint,
  meetingChanged,
  meetingFromRead,
  savedPasswordApplies,
} from './join-again';
import { ActionButtons } from './VirtualParticipantDetails';
import VirtualParticipantList from './VirtualParticipantList';

const { graphqlMock } = vi.hoisted(() => ({ graphqlMock: vi.fn() }));
vi.mock('aws-amplify/api', () => ({ generateClient: () => ({ graphql: graphqlMock }) }));
vi.mock('aws-amplify/utils', () => ({
  ConsoleLogger: class {
    debug() {} // eslint-disable-line class-methods-use-this

    error() {} // eslint-disable-line class-methods-use-this

    warn() {} // eslint-disable-line class-methods-use-this
  },
}));
vi.mock('aws-amplify/auth', () => ({
  fetchAuthSession: vi.fn(),
  fetchUserAttributes: vi.fn(() => Promise.resolve({ email: 'owner@example.com', sub: 'sub-owner' })),
}));
vi.mock('../../contexts/app', () => ({ default: () => ({ user: { username: 'owner@example.com' } }) }));
vi.mock('../../contexts/settings', () => ({ default: () => ({ settings: {} }) }));
vi.mock('../../hooks/use-user-groups', () => ({ default: () => ({ isAdmin: false }) }));
vi.mock('../../hooks/use-meeting-consent-defaults', () => ({ default: () => null }));

const ENDED = {
  id: 'vp-1',
  status: 'COMPLETED',
  CallId: 'call-1',
  Owner: 'owner@example.com',
  meetingName: 'Weekly sync',
  meetingPlatform: 'TEAMS',
  meetingId: '123 456 789',
  hasMeetingPassword: true,
  meetingTime: 1900000000,
  isScheduled: true,
  agenda: 'Introductions\nBudget',
  consentMode: 'opt_in',
  consentRule: 'owner_attests',
  enableVideoRecording: false,
  useStoredZoomAccount: true,
};
const AVAILABLE = { available: true, reason: '' };

describe('joinAgainFormFrom', () => {
  it('copies the meeting details, agenda, consent choice and video recording', () => {
    const { form, consentChoice, enableVideoRecording } = joinAgainFormFrom(ENDED);
    expect(form.meetingName).toBe('Weekly sync');
    expect(form.meetingPlatform).toBe('TEAMS');
    expect(form.meetingId).toBe('123 456 789');
    expect(form.agenda).toBe('Introductions\nBudget');
    expect(consentChoice).toBe('opt_in:owner_attests');
    expect(enableVideoRecording).toBe(false);
  });

  it('leaves the password, date and time empty', () => {
    const { form } = joinAgainFormFrom(ENDED);
    expect(form.meetingPassword).toBe('');
    expect(form.meetingDate).toBe('');
    expect(form.meetingTime).toBe('');
  });

  it('does not carry the earlier Zoom account choice', () => {
    const values = joinAgainFormFrom({ ...ENDED, meetingPlatform: 'ZOOM' });
    expect(values).not.toHaveProperty('useZoomLogin');
  });

  it('starts on Continue only when the meeting can be continued', () => {
    expect(joinAgainFormFrom(ENDED, AVAILABLE).mode).toBe(JOIN_MODE_CONTINUE);
    expect(joinAgainFormFrom(ENDED, { available: false, reason: 'x' }).mode).toBe(JOIN_MODE_NEW);
    expect(joinAgainFormFrom(ENDED).mode).toBe(JOIN_MODE_NEW);
  });

  it('uses the form defaults for settings the earlier VP did not record', () => {
    const values = joinAgainFormFrom({ meetingId: '42', meetingName: 'Old row' });
    expect(values.form.meetingPlatform).toBe('ZOOM');
    expect(values.form.agenda).toBe('');
    expect(values.consentChoice).toBe('default');
    expect(values.enableVideoRecording).toBe(true);
    expect(values.source.hasMeetingPassword).toBe(false);
  });
});

describe('canJoinAgain', () => {
  it('is offered to the owner and Admins once the VP is not waiting for its schedule', () => {
    expect(canJoinAgain(ENDED, true)).toBe(true);
    expect(canJoinAgain({ ...ENDED, status: 'ACTIVE' }, true)).toBe(true);
    expect(canJoinAgain({ ...ENDED, status: 'FAILED' }, true)).toBe(true);
    expect(canJoinAgain({ ...ENDED, status: 'SCHEDULED' }, true)).toBe(false);
    expect(canJoinAgain({ ...ENDED, meetingId: '' }, true)).toBe(false);
  });

  it('is not offered to a viewer who may not manage the VP', () => {
    expect(canJoinAgain(ENDED, false)).toBe(false);
    expect(canJoinAgain(ENDED)).toBe(false);
  });
});

describe('continueOptionFor', () => {
  it('offers Continue for a VP that has left a meeting that still has its transcript', () => {
    ['ENDED', 'COMPLETED', 'FAILED'].forEach((status) => {
      expect(continueOptionFor({ ...ENDED, status }, { CallId: 'call-1' })).toEqual(AVAILABLE);
    });
  });

  it('leaves it to the server when the meeting could not be read', () => {
    expect(continueOptionFor(ENDED, undefined)).toEqual(AVAILABLE);
  });

  it('says why Continue is not offered', () => {
    expect(continueOptionFor({ ...ENDED, CallId: null }, null).reason).toMatch(/never started a meeting/);
    expect(continueOptionFor({ ...ENDED, status: 'ACTIVE' }, {}).reason).toMatch(/still in the meeting/);
    expect(continueOptionFor(ENDED, null)).toEqual({ available: false, reason: NO_MEETING_REASON });
    expect(continueOptionFor(ENDED, { TranscriptDeletedAt: '2026-11-18T10:00:00Z' })).toEqual({
      available: false,
      reason: TRANSCRIPT_DELETED_REASON,
    });
  });
});

describe('meetingFromRead', () => {
  it('reads the record, a missing one, a refusal and a failure', () => {
    expect(meetingFromRead({ data: { getCall: { CallId: 'c' } } })).toEqual({ CallId: 'c' });
    expect(meetingFromRead({ data: { getCall: null } })).toBeNull();
    // getCall refuses a missing meeting as it refuses someone else's.
    expect(meetingFromRead({ error: { errors: [{ errorType: 'AuthorizationError' }] } })).toBeNull();
    expect(meetingFromRead({ error: new Error('Network error') })).toBeUndefined();
  });
});

describe('the choice between Continue and a new meeting', () => {
  const values = joinAgainFormFrom(ENDED, AVAILABLE);
  const state = (form, mode = JOIN_MODE_CONTINUE) => ({ mode, source: values.source, form, changedNote: false });

  it('treats the meeting ID as the form submits it', () => {
    expect(meetingChanged(values.source, { ...values.form, meetingId: '123456789' })).toBe(false);
    expect(meetingChanged(values.source, { ...values.form, meetingId: '987' })).toBe(true);
    expect(meetingChanged(values.source, { ...values.form, meetingPlatform: 'ZOOM' })).toBe(true);
  });

  it('moves to a new meeting, with a note, when the platform, meeting ID or link changes', () => {
    [{ meetingId: '987654321' }, { meetingPlatform: 'ZOOM' }, { meetingId: 'https://teams.microsoft.com/l/x' }].forEach(
      (patch) => {
        const next = joinAgainFormChange(state(values.form), patch);
        expect(next.mode).toBe(JOIN_MODE_NEW);
        expect(next.changedNote).toBe(true);
      },
    );
  });

  it('stays on Continue for an edit that names the same meeting', () => {
    const next = joinAgainFormChange(state(values.form), { meetingId: '123456789' });
    expect(next).toMatchObject({ mode: JOIN_MODE_CONTINUE, changedNote: false });
  });

  it('drops the note once the meeting is the original one again', () => {
    const changed = joinAgainFormChange(state(values.form), { meetingId: '987' });
    const back = joinAgainFormChange({ ...changed, source: values.source }, { meetingId: '123 456 789' });
    expect(back).toMatchObject({ mode: JOIN_MODE_NEW, changedNote: false });
  });

  it('restores the original meeting details and saved settings when Continue is chosen again', () => {
    const edited = { ...values.form, meetingId: '987', meetingPassword: 'typed', meetingName: 'Other', agenda: '' };
    const next = joinAgainModeChange(values, edited, JOIN_MODE_CONTINUE);
    expect(next.mode).toBe(JOIN_MODE_CONTINUE);
    expect(next.form).toEqual(values.form);
    expect(next.form.meetingPassword).toBe('');
    expect(next.consentChoice).toBe('opt_in:owner_attests');
    expect(next.enableVideoRecording).toBe(false);
  });

  it('keeps the edits when a new meeting is chosen', () => {
    const edited = { ...values.form, meetingName: 'Other' };
    expect(joinAgainModeChange(values, edited, JOIN_MODE_NEW)).toEqual({
      form: edited,
      mode: JOIN_MODE_NEW,
      changedNote: false,
    });
  });

  it('labels Continue with the meeting name', () => {
    expect(continueLabel(values.source)).toBe('Continue meeting "Weekly sync"');
  });
});

describe('the saved password', () => {
  const { source, form } = joinAgainFormFrom(ENDED, AVAILABLE);

  it('is used for the same meeting with the password field left empty', () => {
    expect(savedPasswordApplies(source, form)).toBe(true);
    expect(joinAgainPasswordHint(JOIN_MODE_NEW, source, form)).toBe(SAVED_PASSWORD_NEW_HINT);
    expect(joinAgainPasswordHint(JOIN_MODE_CONTINUE, source, form)).toBe(SAVED_PASSWORD_HINT);
  });

  it('gives way to a typed password', () => {
    const typed = { ...form, meetingPassword: 'typed' };
    expect(savedPasswordApplies(source, typed)).toBe(false);
    expect(joinAgainPasswordHint(JOIN_MODE_NEW, source, typed)).toBe('');
  });

  it('is not used for another meeting, where the field behaves as on any create', () => {
    const other = { ...form, meetingId: '987654321' };
    expect(savedPasswordApplies(source, other)).toBe(false);
    expect(joinAgainPasswordHint(JOIN_MODE_NEW, source, other)).toBe('');
  });

  it('is not mentioned when the earlier VP had none', () => {
    const none = { ...source, hasMeetingPassword: false };
    expect(savedPasswordApplies(none, form)).toBe(false);
    expect(joinAgainPasswordHint(JOIN_MODE_CONTINUE, none, form)).toBe('');
  });
});

describe('joinAgainInputFrom', () => {
  it('sends the form values with the source, and an empty password as none', () => {
    const input = joinAgainInputFrom(
      'vp-1',
      {
        meetingName: 'Weekly sync',
        meetingPlatform: 'ZOOM',
        meetingId: '123',
        meetingPassword: '',
        status: 'INITIALIZING',
        enableVideoRecording: true,
        userSub: 'sub-owner',
        consentMode: 'notify',
      },
      { useStoredZoomAccount: true },
    );
    expect(input).toEqual({
      sourceVpId: 'vp-1',
      meetingName: 'Weekly sync',
      meetingPlatform: 'ZOOM',
      meetingId: '123',
      enableVideoRecording: true,
      consentMode: 'notify',
      useStoredZoomAccount: true,
    });
  });

  it('sends a typed password and a schedule', () => {
    const input = joinAgainInputFrom('vp-1', {
      meetingName: 'n',
      meetingPlatform: 'TEAMS',
      meetingId: '1',
      meetingPassword: 'typed',
      meetingTime: 1900000000,
      scheduledTimeZone: 'Europe/Paris',
      isScheduled: true,
    });
    expect(input).toMatchObject({
      meetingPassword: 'typed',
      meetingTime: 1900000000,
      scheduledTimeZone: 'Europe/Paris',
    });
    expect(input).not.toHaveProperty('isScheduled');
    expect(input).not.toHaveProperty('useStoredZoomAccount');
  });
});

describe('joinAgainFromState', () => {
  it('reads the values only from the Join again key', () => {
    const values = joinAgainFormFrom(ENDED);
    expect(joinAgainFromState({ [JOIN_AGAIN_STATE_KEY]: values })).toBe(values);
    expect(joinAgainFromState(null)).toBeNull();
    expect(joinAgainFromState({ other: values })).toBeNull();
  });
});

const renderActions = (vpDetails, onJoinAgain, canManage = true) =>
  render(
    <MemoryRouter>
      <ActionButtons
        vpDetails={vpDetails}
        onRefresh={() => {}}
        onEnd={() => {}}
        onCancelSchedule={() => {}}
        onJoinAgain={onJoinAgain}
        canManage={canManage}
      />
    </MemoryRouter>,
  );

describe('Join again button', () => {
  it('calls onJoinAgain when clicked', () => {
    const onJoinAgain = vi.fn();
    renderActions(ENDED, onJoinAgain);
    fireEvent.click(screen.getByText('Join again'));
    expect(onJoinAgain).toHaveBeenCalledTimes(1);
  });

  it('is not shown to viewers who may not manage the VP', () => {
    renderActions(ENDED, () => {}, false);
    expect(screen.queryByText('Join again')).toBeNull();
  });

  it('is not shown for a VP still waiting for its schedule', () => {
    renderActions({ ...ENDED, status: 'SCHEDULED' }, () => {});
    expect(screen.queryByText('Join again')).toBeNull();
  });

  it('is the only way to rejoin: there is no separate Rejoin button', () => {
    renderActions(ENDED, () => {});
    expect(screen.queryByText('Rejoin')).toBeNull();
  });
});

const StateProbe = () => {
  const location = useLocation();
  return <div data-testid="router-state">{JSON.stringify(location.state)}</div>;
};

const renderListWith = (state, answers = {}) => {
  graphqlMock.mockReset();
  graphqlMock.mockImplementation(({ query }) => {
    if (query.includes('subscription')) return { subscribe: () => ({ unsubscribe: () => {} }) };
    const name = Object.keys(answers).find((key) => query.includes(`${key}(input`));
    if (name) return answers[name]();
    return Promise.resolve({ data: { listVirtualParticipants: [] } });
  });
  render(
    <MemoryRouter initialEntries={[{ pathname: '/virtual-participant', state }]}>
      <Routes>
        <Route
          path="/virtual-participant"
          element={
            <>
              <VirtualParticipantList />
              <StateProbe />
            </>
          }
        />
      </Routes>
    </MemoryRouter>,
  );
};

const joinAgainModal = () =>
  createWrapper(document.body)
    .findAllModals()
    .find((modal) => modal.findHeader().getElement().textContent === 'Join again');

const acknowledge = () =>
  fireEvent.click(screen.getByText(/I will not violate legal, corporate, or ethical restrictions/));

const mutationCalls = (name) =>
  graphqlMock.mock.calls.map(([call]) => call).filter((call) => call.query.includes(`${name}(input`));

describe('VirtualParticipantList with Join again values', () => {
  it('opens on Continue, with the saved password and no scheduling', async () => {
    renderListWith({ [JOIN_AGAIN_STATE_KEY]: joinAgainFormFrom(ENDED, AVAILABLE) });
    expect(await screen.findByDisplayValue('Weekly sync')).toBeTruthy();
    expect(joinAgainModal().isVisible()).toBe(true);
    expect(screen.getByText('Continue meeting "Weekly sync"')).toBeTruthy();
    expect(screen.getByText(SAVED_PASSWORD_HINT)).toBeTruthy();
    expect(screen.queryByText('Schedule for Later')).toBeNull();
    expect(screen.queryByText('Meeting Time (Optional)')).toBeNull();
    // The acknowledgement is asked for again, so Continue waits for it.
    expect(screen.getByText('Continue Meeting').closest('button').disabled).toBe(true);
  });

  it('continues the meeting through rejoin', async () => {
    renderListWith(
      { [JOIN_AGAIN_STATE_KEY]: joinAgainFormFrom(ENDED, AVAILABLE) },
      { rejoinVirtualParticipant: () => Promise.resolve({ data: { rejoinVirtualParticipant: { id: 'vp-1' } } }) },
    );
    await screen.findByDisplayValue('Weekly sync');
    acknowledge();
    fireEvent.click(screen.getByText('Continue Meeting'));
    await waitFor(() => expect(mutationCalls('rejoinVirtualParticipant')).toHaveLength(1));
    expect(mutationCalls('rejoinVirtualParticipant')[0].variables).toEqual({ input: { id: 'vp-1' } });
    expect(mutationCalls('joinAgainVirtualParticipant')).toHaveLength(0);
  });

  it("shows the server's refusal and keeps the form open", async () => {
    renderListWith(
      { [JOIN_AGAIN_STATE_KEY]: joinAgainFormFrom(ENDED, AVAILABLE) },
      {
        // Amplify rejects with the GraphQL response, which is not an Error.
        rejoinVirtualParticipant: () =>
          // eslint-disable-next-line prefer-promise-reject-errors
          Promise.reject({ errors: [{ message: "The Virtual Participant's earlier run is still shutting down." }] }),
      },
    );
    await screen.findByDisplayValue('Weekly sync');
    acknowledge();
    fireEvent.click(screen.getByText('Continue Meeting'));
    expect(await screen.findByText(/earlier run is still shutting down/)).toBeTruthy();
    expect(joinAgainModal().isVisible()).toBe(true);
  });

  it('switches to a new meeting with a note when the meeting ID is edited', async () => {
    renderListWith({ [JOIN_AGAIN_STATE_KEY]: joinAgainFormFrom(ENDED, AVAILABLE) });
    const meetingId = await screen.findByDisplayValue('123 456 789');
    fireEvent.change(meetingId, { target: { value: '987654321' } });
    expect(await screen.findByText(MEETING_CHANGED_NOTE)).toBeTruthy();
    expect(screen.getByText('Schedule for Later')).toBeTruthy();
    expect(screen.queryByText(SAVED_PASSWORD_HINT)).toBeNull();
    expect(screen.queryByText(SAVED_PASSWORD_NEW_HINT)).toBeNull();
  });

  it('starts a new meeting through joinAgain, leaving the password to the server', async () => {
    renderListWith(
      { [JOIN_AGAIN_STATE_KEY]: joinAgainFormFrom(ENDED, AVAILABLE) },
      {
        joinAgainVirtualParticipant: () =>
          Promise.resolve({ data: { joinAgainVirtualParticipant: { id: 'vp-2', Owner: 'owner@example.com' } } }),
      },
    );
    await screen.findByDisplayValue('Weekly sync');
    fireEvent.click(screen.getByText('Start a new meeting'));
    expect(await screen.findByText(SAVED_PASSWORD_NEW_HINT)).toBeTruthy();
    acknowledge();
    fireEvent.click(screen.getByText('Join Now'));
    await waitFor(() => expect(mutationCalls('joinAgainVirtualParticipant')).toHaveLength(1));
    const { input } = mutationCalls('joinAgainVirtualParticipant')[0].variables;
    expect(input).toMatchObject({ sourceVpId: 'vp-1', meetingPlatform: 'TEAMS', meetingId: '123456789' });
    expect(input).not.toHaveProperty('meetingPassword');
    expect(mutationCalls('createVirtualParticipant')).toHaveLength(0);
    expect(mutationCalls('rejoinVirtualParticipant')).toHaveLength(0);
  });

  it('opens on a new meeting, saying why, when the meeting cannot be continued', async () => {
    renderListWith({
      [JOIN_AGAIN_STATE_KEY]: joinAgainFormFrom(ENDED, { available: false, reason: TRANSCRIPT_DELETED_REASON }),
    });
    await screen.findByDisplayValue('Weekly sync');
    expect(screen.getByText(TRANSCRIPT_DELETED_REASON)).toBeTruthy();
    expect(screen.getByText('Join Now')).toBeTruthy();
    expect(screen.getByText('Schedule for Later')).toBeTruthy();
  });

  it('clears the router state so a reload does not open the form again', async () => {
    renderListWith({ [JOIN_AGAIN_STATE_KEY]: joinAgainFormFrom(ENDED, AVAILABLE) });
    await screen.findByDisplayValue('Weekly sync');
    expect(screen.getByTestId('router-state').textContent).toBe('null');
  });

  it('opens no form without Join again values', async () => {
    renderListWith(null);
    expect(joinAgainModal()).toBeUndefined();
    expect(screen.queryByDisplayValue('Weekly sync')).toBeNull();
  });
});
