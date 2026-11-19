/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The meeting password the VP reads at startup: from its own record, or from
 * MEETING_PASSWORD when a launch request queued by an earlier release set it.
 */
import assert from 'node:assert/strict';
import test from 'node:test';

import { resolveMeetingPassword, type MeetingPasswordRow } from './meeting-password-source.js';

const LAUNCHED = { VIRTUAL_PARTICIPANT_ID: 'vp-1', VP_TABLE_NAME: 'stack-VirtualParticipant' };
const noSleep = async (): Promise<void> => {};

function reader(...answers: Array<MeetingPasswordRow | null | Error>) {
  const calls: Array<[string, string]> = [];
  const readRow = async (tableName: string, vpId: string) => {
    calls.push([tableName, vpId]);
    const answer = answers[Math.min(calls.length - 1, answers.length - 1)];
    if (answer instanceof Error) throw answer;
    return answer;
  };
  return { calls, readRow };
}

test('the password is read from the VP record by its id', async () => {
  const { calls, readRow } = reader({ meetingPassword: 'pw-on-the-row' });
  const result = await resolveMeetingPassword({ env: { ...LAUNCHED }, readRow, sleep: noSleep });
  assert.deepEqual(result, { ok: true, password: 'pw-on-the-row', origin: 'record' });
  assert.deepEqual(calls, [['stack-VirtualParticipant', 'vp-1']]);
});

test('a password in the environment is used without reading the record', async () => {
  const { calls, readRow } = reader(new Error('not reached'));
  const result = await resolveMeetingPassword({
    env: { ...LAUNCHED, MEETING_PASSWORD: 'from-env' },
    readRow,
    sleep: noSleep,
  });
  assert.deepEqual(result, { ok: true, password: 'from-env', origin: 'environment' });
  assert.equal(calls.length, 0);
});

test('an empty MEETING_PASSWORD still reads the record', async () => {
  const { calls, readRow } = reader({ meetingPassword: 'pw-on-the-row' });
  const result = await resolveMeetingPassword({
    env: { ...LAUNCHED, MEETING_PASSWORD: '  ' },
    readRow,
    sleep: noSleep,
  });
  assert.deepEqual(result, { ok: true, password: 'pw-on-the-row', origin: 'record' });
  assert.equal(calls.length, 1);
});

test('an empty or absent password on the record is no password', async () => {
  for (const row of [{}, { meetingPassword: '' }]) {
    const { readRow } = reader(row);
    const result = await resolveMeetingPassword({ env: { ...LAUNCHED }, readRow, sleep: noSleep });
    assert.deepEqual(result, { ok: true, password: '', origin: 'none' });
  }
});

test('a failed read is tried again', async () => {
  const { calls, readRow } = reader(new Error('throttled'), { meetingPassword: 'pw' });
  const retries: number[] = [];
  const waits: number[] = [];
  const result = await resolveMeetingPassword({
    env: { ...LAUNCHED },
    readRow,
    delayMs: 10,
    sleep: async (ms) => {
      waits.push(ms);
    },
    onRetry: (attempt) => retries.push(attempt),
  });
  assert.deepEqual(result, { ok: true, password: 'pw', origin: 'record' });
  assert.equal(calls.length, 2);
  assert.deepEqual(retries, [1]);
  assert.deepEqual(waits, [10]);
});

test('a read that keeps failing gives up after the attempts with the reason', async () => {
  const { calls, readRow } = reader(new Error('AccessDeniedException'));
  const waits: number[] = [];
  const result = await resolveMeetingPassword({
    env: { ...LAUNCHED },
    readRow,
    attempts: 4,
    delayMs: 10,
    sleep: async (ms) => {
      waits.push(ms);
    },
  });
  assert.deepEqual(result, { ok: false, error: 'AccessDeniedException' });
  assert.equal(calls.length, 4);
  assert.deepEqual(waits, [10, 20, 40]);
});

test('a record that is not there fails like a read that fails', async () => {
  const { calls, readRow } = reader(null);
  const result = await resolveMeetingPassword({ env: { ...LAUNCHED }, readRow, attempts: 2, sleep: noSleep });
  assert.equal(result.ok, false);
  assert.match((result as { error: string }).error, /no Virtual Participant record has the id vp-1/);
  assert.equal(calls.length, 2);
});

test('without a VP id or table, or in a local run, there is no record to read', async () => {
  for (const env of [
    { VP_TABLE_NAME: 'table' },
    { VIRTUAL_PARTICIPANT_ID: 'vp-1' },
    { ...LAUNCHED, LOCAL_TEST: 'true' },
  ]) {
    const { calls, readRow } = reader(new Error('not reached'));
    const result = await resolveMeetingPassword({ env, readRow, sleep: noSleep });
    assert.deepEqual(result, { ok: true, password: '', origin: 'none' });
    assert.equal(calls.length, 0);
  }
});
