/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Leaving the meeting on the platform before the browser closes (#1127):
 * the click logic against a stand-in page, the budget, which exits leave, and
 * (read from source, since the handlers drive a real browser) how the
 * handlers, the shutdown paths and the container entrypoint are wired to it.
 */

import { strict as assert } from 'node:assert';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import type { ExitInfo, ExitReasonCode } from './details.js';
import {
    LEAVE_BUDGET_MS,
    LEAVE_PLANS,
    clickLeaveStep,
    leaveByClicking,
    leaveWithinBudget,
    shouldLeaveAfterExit,
    type LeaveFrame,
    type LeaveLocator,
    type LeavePage,
} from './meeting-leave.js';

interface FakeControl {
    selector: string;
    label: string;
    visible?: boolean;
    /** Controls that appear once this one is clicked (a confirmation menu). */
    reveals?: FakeControl[];
}

/** A page of controls; a selector matches the controls listed under it. */
function fakePage(controls: FakeControl[]): LeavePage & { clicked: string[] } {
    const shown = [...controls];
    const clicked: string[] = [];
    const locatorFor = (selector: string): LeaveLocator => {
        const matches = () => shown.filter((c) => c.selector === selector);
        const at = (i: number): LeaveLocator => ({
            count: async () => matches().length,
            nth: (n) => at(n),
            isVisible: async () => matches()[i]?.visible !== false,
            getAttribute: async (name) => (name === 'aria-label' ? matches()[i]?.label ?? null : null),
            innerText: async () => '',
            click: async () => {
                const control = matches()[i];
                clicked.push(control.label);
                shown.push(...(control.reveals ?? []));
            },
        });
        return at(0);
    };
    const frame: LeaveFrame = { locator: locatorFor };
    return { clicked, isClosed: () => false, frames: () => [frame] };
}

test('Zoom: clicks Leave, then the Leave Meeting item of the menu it opens', async () => {
    const page = fakePage([
        {
            selector: 'button.footer__leave-btn',
            label: 'Leave',
            reveals: [
                { selector: 'button.leave-meeting-options__btn', label: 'End Meeting for All' },
                { selector: 'button.leave-meeting-options__btn', label: 'Leave Meeting' },
            ],
        },
    ]);
    assert.equal(await leaveByClicking(page, LEAVE_PLANS.ZOOM), true);
    assert.deepEqual(page.clicked, ['Leave', 'Leave Meeting']);
});

test('never ends the meeting for everyone, whichever selector matched', async () => {
    const page = fakePage([
        { selector: '#hangup-button', label: 'End meeting' },
        { selector: 'button[aria-label^="Leave" i]', label: 'Leave (Ctrl+Shift+H)' },
    ]);
    assert.equal(await clickLeaveStep(page, LEAVE_PLANS.TEAMS.leave), 'Leave (Ctrl+Shift+H)');
    assert.deepEqual(page.clicked, ['Leave (Ctrl+Shift+H)']);
});

test('skips a hidden control and a breakout-room leave', async () => {
    const page = fakePage([
        { selector: 'button[aria-label^="Leave" i]', label: 'Leave breakout room' },
        { selector: 'button[aria-label^="Leave" i]', label: 'Leave', visible: false },
        { selector: 'button[data-testid="button"][aria-label^="Leave" i]', label: 'Leave' },
    ]);
    assert.equal(await clickLeaveStep(page, LEAVE_PLANS.CHIME.leave), 'Leave');
    assert.deepEqual(page.clicked, ['Leave']);
});

test('reports no leave when the page has no leave control', async () => {
    const page = fakePage([{ selector: 'button.something-else', label: 'Chat' }]);
    assert.equal(await leaveByClicking(page, LEAVE_PLANS.GOOGLE_MEET), false);
    assert.deepEqual(page.clicked, []);
});

test('a client that leaves without a confirmation still counts as left', async () => {
    const page = fakePage([{ selector: 'button[aria-label="Leave call" i]', label: 'Leave call' }]);
    assert.equal(await leaveByClicking(page, LEAVE_PLANS.GOOGLE_MEET), true);
    assert.deepEqual(page.clicked, ['Leave call']);
});

test('a closed page is not touched', async () => {
    const page = { ...fakePage([]), isClosed: () => true };
    assert.equal(await leaveByClicking(page, LEAVE_PLANS.ZOOM), false);
});

test('the leave is abandoned at its budget, and a failing leave does not throw', async () => {
    const started = Date.now();
    assert.equal(await leaveWithinBudget(() => new Promise(() => {}), 30), 'timeout');
    assert.ok(Date.now() - started < 1_000);
    assert.equal(await leaveWithinBudget(async () => { throw new Error('page gone'); }, 1_000), 'failed');
    assert.equal(await leaveWithinBudget(async () => true, 1_000), 'left');
    assert.equal(await leaveWithinBudget(async () => false, 1_000), 'not-left');
});

test('the leave budget leaves most of the 30-second stop grace period to the rest of the shutdown', () => {
    assert.ok(LEAVE_BUDGET_MS <= 10_000);
});

test('leaves on every exit that finds the VP still in the meeting, and on none that does not', () => {
    const exit = (reason: ExitReasonCode): ExitInfo => ({ reason });
    for (const reason of ['end-command', 'consent-objection', 'alone-in-meeting', 'meeting-timeout', 'unknown'] as const) {
        assert.equal(shouldLeaveAfterExit(exit(reason)), true, reason);
    }
    for (const reason of ['removed-from-meeting', 'host-ended', 'page-closed', 'never-joined'] as const) {
        assert.equal(shouldLeaveAfterExit(exit(reason)), false, reason);
    }
});

// Read from source, with comments dropped and whitespace collapsed, so a
// reformat or a reworded comment does not fail these checks.
const source = (file: string): string =>
    readFileSync(new URL(`../src/${file}`, import.meta.url), 'utf8')
        .replace(/\/\*[\s\S]*?\*\//g, ' ')
        .replace(/^\s*\/\/.*$/gm, ' ')
        .replace(/\s+/g, ' ');

/** The text from `start` up to `end` (both literal), failing when either is missing. */
const between = (body: string, start: string, end: string): string => {
    const from = body.indexOf(start);
    assert.ok(from >= 0, `missing: ${start}`);
    const to = body.indexOf(end, from + start.length);
    assert.ok(to > from, `missing after ${start}: ${end}`);
    return body.slice(from, to);
};

/** Asserts that each needle appears in `body`, in this order. */
const inOrder = (body: string, needles: string[]): void => {
    let at = -1;
    for (const needle of needles) {
        const next = body.indexOf(needle, at + 1);
        assert.ok(next > at, `"${needle}" is not after the previous step`);
        at = next;
    }
};

test('every platform handler has a leave', () => {
    const plans: Record<string, string> = {
        'zoom.ts': 'ZOOM',
        'teams.ts': 'TEAMS',
        'google-meet.ts': 'GOOGLE_MEET',
        'webex.ts': 'WEBEX',
        'chime.ts': 'CHIME',
    };
    for (const [file, plan] of Object.entries(plans)) {
        const method = between(source(file), 'public async leaveMeeting(page: Page)', '}');
        assert.ok(method.includes(`leaveByClicking(page, LEAVE_PLANS.${plan})`), file);
    }
    // The SDK joins leave through the SDK, once, and on their own way out too.
    const zoomSdk = source('zoom-sdk.ts');
    assert.ok(between(zoomSdk, 'public async leaveMeeting(page: Page)', '__lmaLeave').includes('this.leftMeeting'));
    // Asked again after its own exit left, each SDK handler answers the first leave's outcome.
    assert.ok(between(zoomSdk, 'public async leaveMeeting(page: Page)', '__lmaLeave').includes('if (this.leftMeeting) return this.leaveOutcome ?? false;'));
    inOrder(zoomSdk, ['await Promise.race([this.endRequested, timeoutPromise])', 'await this.leaveMeeting(page);']);
    const teamsSdk = source('teams-sdk.ts');
    assert.ok(between(teamsSdk, 'public async leaveMeeting(page: Page)', '}').includes('return this.leaveCall(page);'));
    assert.ok(between(teamsSdk, 'private async leaveCall(page: Page)', '__lmaLeave').includes('this.leftCall = true;'));
    assert.ok(between(teamsSdk, 'private async leaveCall(page: Page)', '__lmaLeave').includes('if (this.leftCall) return this.leaveOutcome ?? false;'));
    // It reports whether it left, so a timed-out or failed leave is not logged as left.
    assert.ok(between(teamsSdk, 'private async leaveCall(page: Page)', 'startCameraWatchdog').includes("this.leaveOutcome = result === 'disconnected';"));
});

test('a stop signal runs the shared teardown, leaving first, then records the status and closes the browser', () => {
    const body = source('index.ts');
    const handler = between(body, 'const signalHandler = async', "process.on('SIGTERM'");
    inOrder(handler, [
        'if (shutdownRequested) {',
        'return;',
        'shutdownRequested = true;',
        'meetingTeardown.run({ stopping: true, leaveReason:',
        'setCompleted(',
        'deregisterFromTargetGroup',
        'releaseServices()',
        'closeAndPersistProfile()',
        'process.exit(0)',
    ]);
    // Nothing the teardown does is done again here.
    for (const step of ['sendEndMeeting', 'stopTranscription', 'recordingService', 'videoRecorder', 'keyframeSampler']) {
        assert.ok(!handler.includes(step), `${step} belongs to the shared teardown`);
    }
    assert.ok(body.includes('leaveActiveMeeting = () => meeting.leaveMeeting(page);'));
});

test('the shared teardown is given every end-of-meeting step', () => {
    const wiring = between(source('index.ts'), 'const meetingTeardown = new MeetingTeardown({', ' }); ');
    for (const step of [
        'stopPageMonitors()',
        'meetingConsent.stop()',
        'meetingConsent.evidenceSettled()',
        'meetingConsent.videoSettled()',
        'leaveMeetingBeforeClose(why)',
        'keyframeSampler.stop()',
        'transcriptionService.stopTranscription()',
        'recordingService.cleanup()',
        'sendCallRecording(url)',
        'videoRecorder.stop()',
        'sendCallVideoRecording(url)',
        'sendEndMeeting()',
    ]) {
        assert.ok(wiring.includes(step), step);
    }
});

test("main()'s cleanup stands aside after a stop signal, and otherwise runs the shared teardown before closing the browser", () => {
    const body = source('index.ts');
    const cleanup = body.slice(body.lastIndexOf('} finally {', body.indexOf('const signalHandler = async')));
    inOrder(cleanup, [
        '} finally {',
        'if (shutdownRequested) {',
        'return;',
        'meetingTeardown.run({ stopping: false, leaveReason: exitInfo && shouldLeaveAfterExit(exitInfo)',
        'releaseServices()',
        'closeAndPersistProfile()',
    ]);
    assert.ok(body.includes('if (statusManager && !shutdownRequested) {'), 'a join failing after a stop signal records no FAILED');
});

test('the ECS entrypoint passes the stop signal to the app and exits with its status', () => {
    const script = readFileSync(new URL('../entrypoint.sh', import.meta.url), 'utf8').replace(/\s+/g, ' ');
    inOrder(script, [
        'node dist/index.js & APP_PID=$!',
        `trap 'kill -TERM "$APP_PID" 2>/dev/null' TERM`,
        `trap 'kill -INT "$APP_PID" 2>/dev/null' INT`,
        'wait "$APP_PID" APP_STATUS=$?',
        'while kill -0 "$APP_PID" 2>/dev/null; do wait "$APP_PID"',
        'if [ "$APP_STATUS" -gt 128 ]; then wait "$APP_PID"',
        'exit "$APP_STATUS"',
    ]);
});
