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
    assert.match(watchdog, /decideDialogAction\([^;]*meetingJoined\(\)/);
});

test('the dialog watchdog decides, and escalates, with the meeting-over flag', () => {
    const watchdog = readFileSync(new URL('../src/dialog-watchdog.ts', import.meta.url), 'utf8');
    assert.match(watchdog, /decideDialogAction\([^;]*meetingOver\(\)\)/);
    assert.match(watchdog, /const escalate = async[\s\S]{0,200}?if \(meetingOver\(\)\)/);
});

test('the meeting is over for the VP once its wait for the end returns', () => {
    const index = readFileSync(new URL('../src/index.ts', import.meta.url), 'utf8');
    assert.match(index, /exitInfo = await Promise\.race\(\[joinMeeting\(\), pageCrashLatch\]\);[\s\S]{0,200}?noteMeetingOver\(\);/);
});

test('a handler attaching the meeting consent flow marks the VP as in the meeting, consent or not', async () => {
    const { attachMeetingConsent } = await import('./consent-runtime.js');
    resetMeetingPhase();
    attachMeetingConsent({} as never, { send: async () => true, leave: () => undefined });
    assert.equal(meetingJoined(), true);
    resetMeetingPhase();
});

test('Zoom and Teams note admission right after their admission check', () => {
    for (const file of ['zoom.ts', 'teams.ts']) {
        const body = readFileSync(new URL(`../src/${file}`, import.meta.url), 'utf8');
        assert.match(body, /if \(!admitted\) \{[\s\S]{0,300}?\n {8}\}\n[^\n]*\n {8}noteMeetingJoined\(\);/, file);
    }
});
