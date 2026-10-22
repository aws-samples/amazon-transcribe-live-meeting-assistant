/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * What the Virtual Participant tells the UI about itself.
 *
 * The VP row is written by several methods, each owning a different part of it,
 * and the mutation behind them leaves out fields it was not given. Two
 * consequences are asserted here: a method that owns one field must send only
 * that field, so it cannot move the lifecycle status; and a detail line written
 * during one phase has to be removed explicitly once it no longer applies,
 * because leaving it out means "keep it".
 *
 * The tests drive the real methods with the signed-request helper stubbed, and
 * inspect the mutation variables that would have gone to AppSync.
 */
import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import {
  HEALTHY_VP_STATUSES,
  TERMINAL_VP_STATUSES,
  VP_STATUS,
  VP_STATUSES,
  VirtualParticipantStatusManager,
  isTerminalVpStatus,
  isVpStatus,
  resumedStatus,
  shouldClearStatusDetail,
  statusToResume,
} from './status-manager.js';

const VP_ID = 'vp-status-test';

interface SentRequest {
  query: string;
  variables: any;
}

/**
 * A manager whose GraphQL transport is replaced by a recorder. The current-row
 * read that some methods perform is answered from `current`; every mutation is
 * recorded and answered with an opaque success.
 */
const harness = (current: Record<string, unknown> = { id: VP_ID, status: VP_STATUS.JOINING }) => {
  process.env.GRAPHQL_ENDPOINT = 'https://example.invalid/graphql';
  const manager: any = new VirtualParticipantStatusManager(VP_ID);
  const sent: SentRequest[] = [];
  manager.signAndSendGraphQLRequest = async (query: string, variables: any) => {
    sent.push({ query, variables });
    if (query.includes('query GetVirtualParticipant')) {
      return { getVirtualParticipant: current };
    }
    return { updateVirtualParticipant: { id: VP_ID } };
  };
  return { manager, sent };
};

const mutations = (sent: SentRequest[]) =>
  sent.filter((request) => request.query.includes('mutation UpdateVirtualParticipant'));

const onlyMutationInput = (sent: SentRequest[]) => {
  const found = mutations(sent);
  assert.equal(found.length, 1, 'expected exactly one mutation');
  return found[0].variables.input;
};

// ---------------------------------------------------------------------------
// The status vocabulary
// ---------------------------------------------------------------------------

test('every status the UI renders is declared in the status union', () => {
  // The UI's STATUS_CONFIG is the only other place the full set appears; these
  // are its keys. A status written by the container that is missing from the
  // UI's map renders as a bare string, and one missing here is a string
  // literal the compiler cannot check.
  const renderedByTheUi = [
    'SCHEDULED',
    'INITIALIZING',
    'WAITING_FOR_CAPACITY',
    'BOOTING',
    'REGISTERING_NETWORK',
    'VNC_READY',
    'HYDRATING_PROFILE',
    'LAUNCHING_BROWSER',
    'WARMING_PROFILE',
    'CONNECTING',
    'JOINING',
    'JOINED',
    'ACTIVE',
    'COMPLETED',
    'FAILED',
    'ENDED',
    'CANCELLED',
    'MANUAL_ACTION_REQUIRED',
  ];
  assert.deepEqual([...VP_STATUSES].sort(), [...renderedByTheUi].sort());
  assert.equal(VP_STATUSES.length, 18);
});

test('healthy and terminal statuses are drawn from the declared set', () => {
  for (const status of [...HEALTHY_VP_STATUSES, ...TERMINAL_VP_STATUSES]) {
    assert.ok(VP_STATUSES.includes(status), `${status} is not a declared status`);
  }
  assert.ok(TERMINAL_VP_STATUSES.every(isTerminalVpStatus));
  assert.equal(isTerminalVpStatus(VP_STATUS.JOINING), false);
  assert.equal(isTerminalVpStatus(VP_STATUS.MANUAL_ACTION_REQUIRED), false);
});

// ---------------------------------------------------------------------------
// Clearing the status detail
// ---------------------------------------------------------------------------

test('shouldClearStatusDetail is true for a healthy transition with no detail', () => {
  for (const status of HEALTHY_VP_STATUSES) {
    assert.equal(shouldClearStatusDetail(status), true, status);
  }
});

test('shouldClearStatusDetail is false when the caller supplies a detail', () => {
  for (const status of HEALTHY_VP_STATUSES) {
    assert.equal(shouldClearStatusDetail(status, 'Asked to leave by Jeremy.'), false, status);
  }
});

test('shouldClearStatusDetail is false for every status that is not healthy', () => {
  const healthy = new Set<string>(HEALTHY_VP_STATUSES);
  for (const status of VP_STATUSES.filter((value) => !healthy.has(value))) {
    assert.equal(shouldClearStatusDetail(status), false, status);
  }
});

test('transition to JOINED clears the status detail', async () => {
  const { manager, sent } = harness();
  await manager.setJoined();
  const input = onlyMutationInput(sent);
  assert.equal(input.status, VP_STATUS.JOINED);
  assert.equal(input.clearErrorMessage, true);
  assert.equal(input.errorMessage, undefined);
});

test('transition to ACTIVE clears the status detail', async () => {
  const { manager, sent } = harness();
  await manager.setActive();
  const input = onlyMutationInput(sent);
  assert.equal(input.status, VP_STATUS.ACTIVE);
  assert.equal(input.clearErrorMessage, true);
});

test('transition to COMPLETED with no exit detail clears the status detail', async () => {
  const { manager, sent } = harness();
  await manager.setCompleted();
  const input = onlyMutationInput(sent);
  assert.equal(input.status, VP_STATUS.COMPLETED);
  assert.equal(input.clearErrorMessage, true);
});

test('transition to COMPLETED with an exit detail writes it instead of clearing', async () => {
  const { manager, sent } = harness();
  await manager.setCompleted('Asked to leave by Jeremy.');
  const input = onlyMutationInput(sent);
  assert.equal(input.errorMessage, 'Asked to leave by Jeremy.');
  assert.equal(input.clearErrorMessage, undefined);
});

test('a JOINING sub-step still delivers its progress message', async () => {
  // errorMessage doubles as the detail channel for in-progress statuses; the
  // clearing behaviour must not take that away.
  const { manager, sent } = harness();
  await manager.setJoiningSubstep('Waiting to be admitted…');
  const input = onlyMutationInput(sent);
  assert.equal(input.status, VP_STATUS.JOINING);
  assert.equal(input.errorMessage, 'Waiting to be admitted…');
  assert.equal(input.clearErrorMessage, undefined);
});

test('a failure writes its message and does not clear it', async () => {
  const { manager, sent } = harness();
  await manager.setFailed('Wrong meeting password');
  const input = onlyMutationInput(sent);
  assert.equal(input.status, VP_STATUS.FAILED);
  assert.equal(input.errorMessage, 'Wrong meeting password');
  assert.equal(input.clearErrorMessage, undefined);
});

// ---------------------------------------------------------------------------
// The structured exit trigger
// ---------------------------------------------------------------------------

test('a terminal status persists the structured exit trigger alongside the prose', async () => {
  const { manager, sent } = harness();
  await manager.setFailed('Could not join the meeting.', 'pre-join:not-admitted');
  const input = onlyMutationInput(sent);
  assert.equal(input.endTrigger, 'pre-join:not-admitted');
  assert.equal(input.errorMessage, 'Could not join the meeting.');
});

test('a status write with no trigger leaves the trigger field alone', async () => {
  const { manager, sent } = harness();
  await manager.setJoining();
  assert.equal(onlyMutationInput(sent).endTrigger, undefined);
});

// ---------------------------------------------------------------------------
// Writers that own one field
// ---------------------------------------------------------------------------

test('setCallId sends the CallId and no status', async () => {
  const { manager, sent } = harness({ id: VP_ID, status: VP_STATUS.ACTIVE });
  await manager.setCallId('call-1234');
  const input = onlyMutationInput(sent);
  assert.equal(input.CallId, 'call-1234');
  assert.equal(input.status, undefined, 'setCallId must not write a status');
  assert.deepEqual(Object.keys(input).sort(), ['CallId', 'id']);
});

test('clearManualAction leaves the status alone on a VP that is not prompting', async () => {
  // Several callers clear opportunistically, without knowing whether a prompt
  // was ever raised; one of those must not move a live VP.
  const { manager, sent } = harness({ id: VP_ID, status: VP_STATUS.ACTIVE });
  await manager.clearManualAction();
  const input = onlyMutationInput(sent);
  assert.equal(input.clearManualAction, true, 'the prompt must be cleared explicitly');
  assert.equal(input.status, undefined, 'a VP that is not prompting keeps its status');
  assert.deepEqual(Object.keys(input).sort(), ['clearManualAction', 'id']);
});

test('clearManualAction resumes the status the prompt interrupted', async () => {
  // MANUAL_ACTION_REQUIRED is a status, and the UI's alert stays up until the
  // status leaves it — so clearing a prompt does put a status back, and the one
  // it puts back is whatever the prompt displaced.
  for (const interrupted of [VP_STATUS.CONNECTING, VP_STATUS.JOINING, VP_STATUS.ACTIVE]) {
    const { manager, sent } = harness({
      id: VP_ID,
      status: VP_STATUS.MANUAL_ACTION_REQUIRED,
      statusBeforeManualAction: interrupted,
    });
    await manager.clearManualAction();
    const input = onlyMutationInput(sent);
    assert.equal(input.status, interrupted);
    assert.equal(input.clearManualAction, true);
  }
});

test('clearManualAction falls back to JOINING when no interrupted status was recorded', async () => {
  // A row written before the interrupted status was recorded, or one whose
  // recorded value is not a status this build knows.
  for (const recorded of [undefined, null, 'SOMETHING_ELSE', VP_STATUS.MANUAL_ACTION_REQUIRED]) {
    const { manager, sent } = harness({
      id: VP_ID,
      status: VP_STATUS.MANUAL_ACTION_REQUIRED,
      statusBeforeManualAction: recorded,
    });
    await manager.clearManualAction();
    assert.equal(onlyMutationInput(sent).status, VP_STATUS.JOINING, String(recorded));
  }
});

test('setManualActionRequired records the status it is interrupting', async () => {
  const { manager, sent } = harness({ id: VP_ID, status: VP_STATUS.ACTIVE });
  await manager.setManualActionRequired('CONSENT', 'Dismiss the dialog', 180);
  const input = onlyMutationInput(sent);
  assert.equal(input.status, VP_STATUS.MANUAL_ACTION_REQUIRED);
  assert.equal(input.statusBeforeManualAction, VP_STATUS.ACTIVE);
  assert.equal(input.manualActionType, 'CONSENT');
  assert.equal(input.manualActionMessage, 'Dismiss the dialog');
  assert.equal(input.manualActionTimeoutSeconds, 180);
});

test('a second prompt does not overwrite the recorded interrupted status', async () => {
  // Otherwise the recorded value becomes MANUAL_ACTION_REQUIRED and clearing
  // the prompt would have nowhere to go back to.
  const { manager, sent } = harness({
    id: VP_ID,
    status: VP_STATUS.MANUAL_ACTION_REQUIRED,
    statusBeforeManualAction: VP_STATUS.JOINING,
  });
  await manager.setManualActionRequired('CAPTCHA', 'Solve the CAPTCHA', 300);
  assert.equal(onlyMutationInput(sent).statusBeforeManualAction, undefined);
});

test('no status write rewinds a VP that is already further along', async () => {
  // The two methods that used to carry a hardcoded status are the ones a live
  // VP can reach while it is mid-meeting. Neither may move it: setCallId never
  // writes a status at all, and clearing a prompt only ever restores one.
  for (const call of [
    (manager: any) => manager.setCallId('call-1234'),
    (manager: any) => manager.clearManualAction(),
  ]) {
    const { manager, sent } = harness({ id: VP_ID, status: VP_STATUS.ACTIVE });
    await call(manager);
    assert.equal(onlyMutationInput(sent).status, undefined);
  }
});

// ---------------------------------------------------------------------------
// Resume bookkeeping
// ---------------------------------------------------------------------------

test('isVpStatus accepts the declared statuses and nothing else', () => {
  for (const status of VP_STATUSES) {
    assert.equal(isVpStatus(status), true, status);
  }
  for (const value of [undefined, null, '', 'joined', 'NOT_A_STATUS']) {
    assert.equal(isVpStatus(value as any), false, String(value));
  }
});

test('statusToResume records a real status and declines everything else', () => {
  assert.equal(statusToResume(VP_STATUS.ACTIVE), VP_STATUS.ACTIVE);
  assert.equal(statusToResume(VP_STATUS.MANUAL_ACTION_REQUIRED), null);
  assert.equal(statusToResume('NOT_A_STATUS'), null);
  assert.equal(statusToResume(undefined), null);
});

test('resumedStatus never returns MANUAL_ACTION_REQUIRED', () => {
  // Returning it would leave the VP prompting with no prompt on the row.
  assert.notEqual(resumedStatus(VP_STATUS.MANUAL_ACTION_REQUIRED), VP_STATUS.MANUAL_ACTION_REQUIRED);
  for (const status of VP_STATUSES) {
    assert.notEqual(resumedStatus(status), VP_STATUS.MANUAL_ACTION_REQUIRED, status);
  }
});
