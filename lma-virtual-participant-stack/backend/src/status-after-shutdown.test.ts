/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * What the Virtual Participant writes once it has been asked to stop.
 *
 * Shutdown is process state, so these tests live in their own file: node's
 * test runner gives each file its own process, and the flag set here cannot
 * reach the tests in `vp-status-updates.test.ts`.
 *
 * The tests drive the real methods with the signed-request helper stubbed, and
 * inspect the requests that would have gone to AppSync.
 */
import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import { readFileSync } from 'node:fs';
import { VP_STATUS, VirtualParticipantStatusManager, beginShutdown } from './status-manager.js';

const VP_ID = 'vp-shutdown-test';

interface SentRequest {
  query: string;
  variables: any;
}

const PROMPTING_ROW = {
  id: VP_ID,
  status: VP_STATUS.MANUAL_ACTION_REQUIRED,
  statusBeforeManualAction: VP_STATUS.JOINING,
};

/**
 * A manager whose GraphQL transport is replaced by a recorder. `respond` can
 * answer a request itself; otherwise the row read is answered from `current`
 * and every mutation with an opaque success. `module` selects which copy of
 * the status-manager module the manager comes from.
 */
const harness = (
  respond?: (query: string) => unknown,
  current: Record<string, unknown> = { id: VP_ID, status: VP_STATUS.HYDRATING_PROFILE },
  module: { VirtualParticipantStatusManager: typeof VirtualParticipantStatusManager } = {
    VirtualParticipantStatusManager,
  },
) => {
  process.env.GRAPHQL_ENDPOINT = 'https://example.invalid/graphql';
  const manager: any = new module.VirtualParticipantStatusManager(VP_ID);
  const sent: SentRequest[] = [];
  manager.signAndSendGraphQLRequest = async (query: string, variables: any) => {
    sent.push({ query, variables });
    if (respond) {
      const answer = respond(query);
      if (answer !== undefined) return answer;
    }
    if (query.includes('query GetVirtualParticipant')) {
      return { getVirtualParticipant: current };
    }
    return { updateVirtualParticipant: { id: VP_ID } };
  };
  return { manager, sent };
};

const mutations = (sent: SentRequest[]) =>
  sent.filter((request) => request.query.includes('mutation UpdateVirtualParticipant'));

const declineMutations = (query: string) => {
  if (query.includes('mutation UpdateVirtualParticipant')) {
    throw new Error('GraphQL errors: [{"errorType":"DynamoDB:ConditionalCheckFailedException"}]');
  }
  return undefined;
};

/**
 * A fresh copy of the status-manager module, with shutdown state of its own,
 * so each method's check after its row read can be tested in a module that has
 * not yet begun to shut down. The query string gives the copy its own module
 * record.
 */
let freshCopies = 0;
const freshModule = async (): Promise<any> => import(`./status-manager.js?fresh=${++freshCopies}`);

// The tests before `beginShutdown()` is first called on the shared module run
// in a process that has not begun to shut down; node runs them in order.

test('a progress status declined because the VP was ended is reported, not thrown', async () => {
  // The resolver conditions a progress status on the row not having been
  // ended. A write that was already in flight when the stop arrived comes back
  // as a conditional-check failure, which is an expected outcome.
  const { manager, sent } = harness(declineMutations);
  assert.equal(await manager.setLaunchingBrowser(), false);
  assert.equal(mutations(sent).length, 1);
});

test('the same decline on a terminal status is still a failure', async () => {
  // Terminal statuses are not conditioned on the row's end, so a conditional
  // failure there is not the expected outcome above and is not hidden as one.
  const { manager } = harness(declineMutations);
  assert.equal(await manager.setCompleted(), false);
});

test('a write whose row read is in flight when shutdown begins writes nothing', async () => {
  // The observed shape: the read for a progress write is in flight when the
  // signal arrives, so the check on entry has already passed. Each method
  // checks again once its read returns.
  for (const [name, write, current] of [
    [
      'a progress status',
      (m: any) => m.setLaunchingBrowser(),
      { id: VP_ID, status: VP_STATUS.HYDRATING_PROFILE },
    ],
    [
      'a prompt',
      (m: any) => m.setManualActionRequired('CAPTCHA', 'Solve it', 300),
      { id: VP_ID, status: VP_STATUS.JOINING },
    ],
    ['the clearing of a prompt', (m: any) => m.clearManualAction(), PROMPTING_ROW],
  ] as const) {
    const fresh = await freshModule();
    const { manager, sent } = harness(
      (query) => {
        if (query.includes('query GetVirtualParticipant')) {
          fresh.beginShutdown();
          return { getVirtualParticipant: current };
        }
        return undefined;
      },
      current,
      fresh,
    );
    assert.equal(await write(manager), false, name);
    assert.equal(sent.length, 1, `${name}: the read should have been sent`);
    assert.equal(mutations(sent).length, 0, `${name}: no mutation should follow it`);
  }
});

test('shutdown is process state, seen by managers created before and after it', async () => {
  // The platform handlers create a manager of their own for each write.
  const before = harness();
  beginShutdown();
  const after = harness();
  for (const { manager, sent } of [before, after]) {
    assert.equal(await manager.setJoining(), false);
    assert.equal(sent.length, 0);
  }
});

test('after shutdown has begun, no progress status is written', async () => {
  for (const write of [
    (m: any) => m.setInitializing(),
    (m: any) => m.setRegisteringNetwork(),
    (m: any) => m.setLaunchingBrowser(),
    (m: any) => m.setHydratingProfile(),
    (m: any) => m.setWarmingProfile(),
    (m: any) => m.setConnecting(),
    (m: any) => m.setJoining(),
    (m: any) => m.setJoined(),
    (m: any) => m.setActive(),
  ]) {
    const { manager, sent } = harness();
    assert.equal(await write(manager), false);
    assert.equal(sent.length, 0, 'nothing should be sent, not even the read');
  }
});

test('after shutdown has begun, no join sub-step, prompt or prompt clearing is written', async () => {
  const { manager, sent } = harness(undefined, PROMPTING_ROW);
  await manager.setJoiningSubstep('Waiting to be admitted…');
  assert.equal(await manager.setManualActionRequired('CAPTCHA', 'Solve it', 300), false);
  assert.equal(await manager.clearManualAction(), false);
  assert.equal(sent.length, 0);
});

test('after shutdown has begun, terminal statuses are still written', async () => {
  for (const [write, expected] of [
    [(m: any) => m.setCompleted(), VP_STATUS.COMPLETED],
    [(m: any) => m.setFailed('Uncaught exception: boom'), VP_STATUS.FAILED],
  ] as const) {
    const { manager, sent } = harness();
    assert.equal(await write(manager), true);
    const found = mutations(sent);
    assert.equal(found.length, 1);
    assert.equal(found[0].variables.input.status, expected);
  }
});

test('both shutdown paths begin the shutdown before their first await', () => {
  // The signal handler and the emergency cleanup each await network calls
  // before writing their terminal status, and the setup or join code keeps
  // running during those awaits. Beginning the shutdown first is what keeps
  // that code from reporting progress in the meantime. Neither depends on the
  // main status manager existing.
  const source = readFileSync(new URL('../src/index.ts', new URL('.', import.meta.url)), 'utf8');
  for (const start of ['const signalHandler = async', 'const emergencyCleanup = async']) {
    const at = source.indexOf(start);
    assert.ok(at >= 0, `${start} was not found in index.ts`);
    const body = source.slice(at);
    const begin = body.indexOf('\n    beginShutdown();');
    const firstAwait = body.indexOf('await ');
    assert.ok(begin >= 0 && begin < firstAwait, `${start} does not begin the shutdown first`);
  }
});
