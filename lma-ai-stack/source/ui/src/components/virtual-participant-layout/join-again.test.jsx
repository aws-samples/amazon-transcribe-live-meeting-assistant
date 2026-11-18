/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import React from 'react';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import { fireEvent, render, screen } from '@testing-library/react';
import { describe, it, expect, vi } from 'vitest';
import createWrapper from '@cloudscape-design/components/test-utils/dom';

import {
  JOIN_AGAIN_STATE_KEY,
  PASSWORD_NOT_COPIED_HINT,
  canJoinAgain,
  joinAgainFormFrom,
  joinAgainFromState,
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
vi.mock('aws-amplify/auth', () => ({ fetchAuthSession: vi.fn(), fetchUserAttributes: vi.fn() }));
vi.mock('../../contexts/app', () => ({ default: () => ({ user: { username: 'me@example.com' } }) }));
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

  it('says the password was not copied only when the earlier VP had one the form asks for', () => {
    expect(joinAgainFormFrom(ENDED).passwordNotCopied).toBe(true);
    expect(joinAgainFormFrom({ ...ENDED, hasMeetingPassword: false }).passwordNotCopied).toBe(false);
    expect(joinAgainFormFrom({ ...ENDED, hasMeetingPassword: null }).passwordNotCopied).toBe(false);
    // A join link carries its password; the form hides the field for one.
    expect(joinAgainFormFrom({ ...ENDED, meetingId: 'https://teams.microsoft.com/l/x' }).passwordNotCopied).toBe(false);
    expect(
      joinAgainFormFrom({ ...ENDED, meetingPlatform: 'GOOGLE_MEET', meetingId: 'abc-defg-hij' }).passwordNotCopied,
    ).toBe(false);
  });

  it('uses the form defaults for settings the earlier VP did not record', () => {
    const values = joinAgainFormFrom({ meetingId: '42', meetingName: 'Old row' });
    expect(values.form.meetingPlatform).toBe('ZOOM');
    expect(values.form.agenda).toBe('');
    expect(values.consentChoice).toBe('default');
    expect(values.enableVideoRecording).toBe(true);
  });
});

describe('canJoinAgain', () => {
  it('is offered for a VP that is no longer waiting for its schedule', () => {
    expect(canJoinAgain(ENDED)).toBe(true);
    expect(canJoinAgain({ ...ENDED, status: 'ACTIVE' })).toBe(true);
    expect(canJoinAgain({ ...ENDED, status: 'FAILED' })).toBe(true);
    expect(canJoinAgain({ ...ENDED, status: 'SCHEDULED' })).toBe(false);
    expect(canJoinAgain({ ...ENDED, meetingId: '' })).toBe(false);
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

const renderActions = (vpDetails, onJoinAgain) =>
  render(
    <MemoryRouter>
      <ActionButtons
        vpDetails={vpDetails}
        onRefresh={() => {}}
        onEnd={() => {}}
        onCancelSchedule={() => {}}
        onJoinAgain={onJoinAgain}
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

  it('is shown to viewers who may not manage the VP', () => {
    // renderActions passes no canManage, so End and Cancel Schedule are hidden.
    renderActions({ ...ENDED, status: 'ACTIVE' }, () => {});
    expect(screen.queryByText('End Virtual Participant')).toBeNull();
    expect(screen.getByText('Join again')).toBeTruthy();
  });

  it('is not shown for a VP still waiting for its schedule', () => {
    renderActions({ ...ENDED, status: 'SCHEDULED' }, () => {});
    expect(screen.queryByText('Join again')).toBeNull();
  });
});

const StateProbe = () => {
  const location = useLocation();
  return <div data-testid="router-state">{JSON.stringify(location.state)}</div>;
};

const renderListWith = (state) => {
  graphqlMock.mockReset();
  graphqlMock.mockImplementation(({ query }) =>
    query.includes('subscription')
      ? { subscribe: () => ({ unsubscribe: () => {} }) }
      : Promise.resolve({ data: { listVirtualParticipants: [] } }),
  );
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

const createModal = () =>
  createWrapper(document.body)
    .findAllModals()
    .find((modal) => modal.findHeader().getElement().textContent === 'Create Virtual Participant');

describe('VirtualParticipantList with Join again values', () => {
  it('opens the create form filled in, with the password hint', async () => {
    renderListWith({ [JOIN_AGAIN_STATE_KEY]: joinAgainFormFrom(ENDED) });
    expect(await screen.findByDisplayValue('Weekly sync')).toBeTruthy();
    expect(createModal().isVisible()).toBe(true);
    expect(screen.getByDisplayValue('123 456 789')).toBeTruthy();
    expect(screen.getByPlaceholderText('Enter meeting password if required').value).toBe('');
    expect(screen.getByText(PASSWORD_NOT_COPIED_HINT)).toBeTruthy();
    // The acknowledgement is asked for again, so Join Now waits for it.
    expect(screen.getByText('Join Now').closest('button').disabled).toBe(true);
  });

  it('clears the router state so a reload does not open the form again', async () => {
    renderListWith({ [JOIN_AGAIN_STATE_KEY]: joinAgainFormFrom(ENDED) });
    await screen.findByDisplayValue('Weekly sync');
    expect(screen.getByTestId('router-state').textContent).toBe('null');
  });

  it('opens no form without Join again values', async () => {
    renderListWith(null);
    expect(createModal().isVisible()).toBe(false);
    expect(screen.queryByDisplayValue('Weekly sync')).toBeNull();
    expect(screen.queryByText(PASSWORD_NOT_COPIED_HINT)).toBeNull();
  });
});
