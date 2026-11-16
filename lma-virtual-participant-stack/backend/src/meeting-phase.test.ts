/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { meetingJoined, noteMeetingJoined, resetMeetingPhase } from './meeting-phase.js';

test('the VP counts as in the meeting from when it is admitted', () => {
    resetMeetingPhase();
    assert.equal(meetingJoined(), false);
    noteMeetingJoined();
    assert.equal(meetingJoined(), true);
    resetMeetingPhase();
});

test('the dialog watchdog decides with the admission flag', () => {
    // The one wiring that cannot be driven without a page: the watchdog's call.
    const watchdog = readFileSync(new URL('../src/dialog-watchdog.ts', import.meta.url), 'utf8');
    assert.match(watchdog, /decideDialogAction\([^)]*meetingJoined\(\)\)/);
});

test('a handler attaching the meeting consent flow marks the VP as in the meeting, consent or not', async () => {
    const { attachMeetingConsent } = await import('./consent-runtime.js');
    resetMeetingPhase();
    attachMeetingConsent({} as never, { send: async () => true, leave: () => undefined });
    assert.equal(meetingJoined(), true);
    resetMeetingPhase();
});
