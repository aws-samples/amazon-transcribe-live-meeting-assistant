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
import { readFileSync, readdirSync } from 'node:fs';
import {
  HEALTHY_VP_STATUSES,
  TERMINAL_VP_STATUSES,
  VP_STATUS,
  VP_STATUSES,
  VirtualParticipantStatusManager,
  isConditionalCheckFailure,
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

/**
 * The statuses the React UI has rendering for, read from its source.
 *
 * Read rather than copied on purpose. A hardcoded list here would be a second
 * piece of bookkeeping: adding a status to this union and to that list would
 * keep this test green while the UI still had no rendering for it. Comparing
 * against the other file means one side cannot move without the other.
 */
const statusesRenderedByTheUi = (): string[] => {
  // src/ and dist/ are siblings, so one relative path serves both the ts-node
  // and the compiled `node --test dist/*.test.js` runs.
  const url = new URL(
    '../../../lma-ai-stack/source/ui/src/components/virtual-participant-layout/VirtualParticipantDetails.jsx',
    new URL('.', import.meta.url),
  );
  const source = readFileSync(url, 'utf8');
  const start = source.indexOf('const STATUS_CONFIG = {');
  assert.ok(start >= 0, 'STATUS_CONFIG was not found — the path in this test needs updating');
  const block = source.slice(start, source.indexOf('\n};', start));
  return [...block.matchAll(/^ {2}([A-Z_]+): \{$/gm)].map((match) => match[1]);
};

test('the status union and the UI status map describe the same statuses', () => {
  // A status the container writes that the UI does not know renders as a bare
  // string with no icon or colour; one the UI knows that the container cannot
  // name is a status nothing can reach.
  const rendered = statusesRenderedByTheUi();
  // Guards the parser, not the vocabulary: a regex that stopped matching would
  // otherwise make this pass by comparing nothing. The size of the set is
  // asserted only here, so adding a status means editing one count.
  assert.equal(rendered.length, 18, 'the UI status map was not parsed as expected');
  assert.deepEqual([...VP_STATUSES].sort(), [...rendered].sort());
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
  assert.equal(input.errorMessage, 'Waiting to be admitted…');
  assert.equal(input.clearErrorMessage, undefined);
});

test('a JOINING sub-step writes no status', async () => {
  // The row is already JOINING when the handler starts, so a sub-step has only
  // its message to add — and the dialog watchdog runs concurrently with the
  // join, so a status here could take a manual-action prompt off the screen
  // while the user is still needed to clear it.
  const { manager, sent } = harness({ id: VP_ID, status: VP_STATUS.MANUAL_ACTION_REQUIRED });
  await manager.setJoiningSubstep('Waiting to be admitted…');
  const input = onlyMutationInput(sent);
  assert.equal(input.status, undefined);
  assert.deepEqual(Object.keys(input).sort(), ['errorMessage', 'id']);
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

test('a VP that stopped prompting mid-clear keeps the status that overtook it', async () => {
  // The status to resume comes from a read, so the write is conditional on the
  // row still prompting (see the resolver). DynamoDB declining it is the
  // expected outcome, not a fault: something more current than this read has
  // already written a status.
  process.env.GRAPHQL_ENDPOINT = 'https://example.invalid/graphql';
  const manager: any = new VirtualParticipantStatusManager(VP_ID);
  manager.signAndSendGraphQLRequest = async (query: string) => {
    if (query.includes('query GetVirtualParticipant')) {
      return {
        getVirtualParticipant: {
          id: VP_ID,
          status: VP_STATUS.MANUAL_ACTION_REQUIRED,
          statusBeforeManualAction: VP_STATUS.JOINING,
        },
      };
    }
    throw new Error(
      'GraphQL errors: [{"errorType":"DynamoDB:ConditionalCheckFailedException"}]',
    );
  };
  assert.equal(await manager.clearManualAction(), true);
});

test('a VP that started prompting mid-clear keeps its prompt', async () => {
  // The opportunistic callers clear without knowing whether a prompt was ever
  // raised, so they read a row that is not prompting. If one is raised between
  // that read and the write, removing its description would leave a row
  // reporting a prompt that nothing can describe — so the resolver's converse
  // condition declines the write, and that is the expected outcome here too.
  process.env.GRAPHQL_ENDPOINT = 'https://example.invalid/graphql';
  const manager: any = new VirtualParticipantStatusManager(VP_ID);
  let sentInput: any;
  manager.signAndSendGraphQLRequest = async (query: string, variables: any) => {
    if (query.includes('query GetVirtualParticipant')) {
      return { getVirtualParticipant: { id: VP_ID, status: VP_STATUS.ACTIVE } };
    }
    sentInput = variables.input;
    throw new Error('GraphQL errors: [{"errorType":"DynamoDB:ConditionalCheckFailedException"}]');
  };
  assert.equal(await manager.clearManualAction(), true);
  // No status was sent, which is what selects the converse condition.
  assert.equal(sentInput.status, undefined);
  assert.equal(sentInput.clearManualAction, true);
});

test('isConditionalCheckFailure tells that outcome from a real failure', () => {
  assert.equal(
    isConditionalCheckFailure(new Error('DynamoDB:ConditionalCheckFailedException')),
    true,
  );
  assert.equal(isConditionalCheckFailure(new Error('conditionalcheckfailed')), true);
  assert.equal(isConditionalCheckFailure(new Error('AccessDeniedException')), false);
  assert.equal(isConditionalCheckFailure(new Error('fetch failed')), false);
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
  // Both are reachable while a VP is mid-meeting, and neither may move it:
  // setCallId never writes a status, and clearing a prompt only restores one.
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
// What a write tells its subscribers
// ---------------------------------------------------------------------------

/** This file's own source, for assertions about the mutations it declares. */
const statusManagerSource = (): string =>
  readFileSync(new URL('../src/status-manager.ts', new URL('.', import.meta.url)), 'utf8');

/** The fields the UI's VP subscription asks for, read from its source. */
const subscribedFields = (): string[] => {
  const url = new URL(
    '../../../lma-ai-stack/source/ui/src/components/virtual-participant-layout/ManualActionAlerts.jsx',
    new URL('.', import.meta.url),
  );
  const source = readFileSync(url, 'utf8');
  const start = source.indexOf('const ON_UPDATE_VP');
  assert.ok(start >= 0, 'ON_UPDATE_VP was not found — the path in this test needs updating');
  const block = source.slice(start, source.indexOf('`;', start));
  return [...block.matchAll(/^ {6}(\w+)$/gm)].map((match) => match[1]);
};

test('every mutation selects at least the fields the UI subscribes to', () => {
  // An AppSync subscription delivers what the *mutation* selected: a field the
  // subscriber asked for and the mutation did not arrives as null. The alert
  // component rebuilds its alert from that payload rather than merging it, so a
  // write narrower than the subscription would have it rebuild a live prompt
  // from nulls — generic text in place of the real message, and the desktop
  // notification fired again. Sharing one selection set is what prevents that;
  // this asserts the shared set covers the subscription and that every mutation
  // uses it.
  const source = statusManagerSource();
  const subscribed = subscribedFields();
  assert.ok(subscribed.length >= 10, `only parsed ${subscribed.length} subscribed fields`);

  const start = source.indexOf('const SUBSCRIBED_FIELDS = `');
  assert.ok(start >= 0, 'SUBSCRIBED_FIELDS was not found');
  const shared = source.slice(start, source.indexOf('`;', start));
  const missing = subscribed.filter((field) => !new RegExp(`^\\s+${field}$`, 'm').test(shared));
  assert.deepEqual(missing, [], 'SUBSCRIBED_FIELDS does not cover the subscription');

  const mutations = source.split('mutation UpdateVirtualParticipant').slice(1);
  assert.equal(mutations.length, 6, 'the mutation count in this test needs updating');
  for (const [index, mutation] of mutations.entries()) {
    const selection = mutation.slice(0, mutation.indexOf('`;'));
    assert.ok(
      selection.includes('${SUBSCRIBED_FIELDS}'),
      `mutation ${index + 1} declares its own selection set instead of the shared one`,
    );
  }
});

test('the status manager is the only writer in the container source', () => {
  // The state machine, the task reaper, entrypoint.sh and the UI all write the
  // row too; this is about the container, where a second implementation is how
  // a field ends up written by code that does not own it.
  const here = new URL('.', import.meta.url);
  const sources = readdirSync(new URL('../src/', here)).filter(
    (name) => name.endsWith('.ts') && !name.endsWith('.test.ts') && name !== 'status-manager.ts',
  );
  assert.ok(sources.length > 10, 'the source listing in this test needs updating');
  const offenders = sources.filter((name) =>
    readFileSync(new URL(`../src/${name}`, here), 'utf8').includes(
      'mutation UpdateVirtualParticipant',
    ),
  );
  assert.deepEqual(offenders, [], 'these files write the VP record directly');
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
