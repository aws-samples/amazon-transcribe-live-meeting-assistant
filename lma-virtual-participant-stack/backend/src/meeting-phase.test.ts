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

test('admission is noted on the JOINED and ACTIVE statuses, and the watchdog reads it', () => {
    const status = readFileSync(new URL('../src/status-manager.ts', import.meta.url), 'utf8');
    assert.match(status, /if \(status === VP_STATUS\.JOINED \|\| status === VP_STATUS\.ACTIVE\) noteMeetingJoined\(\);/);
    const watchdog = readFileSync(new URL('../src/dialog-watchdog.ts', import.meta.url), 'utf8');
    assert.match(watchdog, /decideDialogAction\(analysis, opts\.platform, actionText, meetingJoined\(\)\)/);
});
