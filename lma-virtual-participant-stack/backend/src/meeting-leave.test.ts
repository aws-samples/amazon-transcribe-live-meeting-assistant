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

const source = (file: string): string => readFileSync(new URL(`../src/${file}`, import.meta.url), 'utf8');

test('every platform handler has a leave', () => {
    const plans: Record<string, string> = {
        'zoom.ts': 'ZOOM',
        'teams.ts': 'TEAMS',
        'google-meet.ts': 'GOOGLE_MEET',
        'webex.ts': 'WEBEX',
        'chime.ts': 'CHIME',
    };
    for (const [file, plan] of Object.entries(plans)) {
        assert.match(
            source(file),
            new RegExp(`public async leaveMeeting\\(page: Page\\): Promise<boolean> \\{\\s*return leaveByClicking\\(page, LEAVE_PLANS\\.${plan}\\);`),
            file,
        );
    }
    // The SDK joins leave through the SDK, once, on their own way out too.
    const zoomSdk = source('zoom-sdk.ts');
    assert.match(zoomSdk, /public async leaveMeeting\(page: Page\)[\s\S]{0,200}if \(page\.isClosed\(\) \|\| this\.leftMeeting\) return false;/);
    assert.match(zoomSdk, /const exitInfo = await Promise\.race\(\[this\.endRequested, timeoutPromise\]\);[\s\S]{0,200}await this\.leaveMeeting\(page\);/);
    const teamsSdk = source('teams-sdk.ts');
    assert.match(teamsSdk, /public async leaveMeeting\(page: Page\): Promise<boolean> \{\s*await this\.leaveCall\(page\);/);
    assert.match(teamsSdk, /if \(page\.isClosed\(\) \|\| this\.leftCall\) return;\s*this\.leftCall = true;/);
});

test('a stop signal leaves the meeting before anything else in its shutdown', () => {
    const body = source('index.ts');
    const handler = body.slice(body.indexOf('const signalHandler = async'), body.indexOf("process.on('SIGTERM'"));
    const leave = handler.indexOf('await leaveMeetingBeforeClose(');
    assert.ok(leave > 0, 'the signal handler leaves the meeting');
    for (const later of ['deregisterFromTargetGroup', 'sendEndMeeting', 'setCompleted', 'stopTranscription', 'closeAndPersistProfile']) {
        assert.ok(handler.indexOf(later) > leave, `${later} comes after the leave`);
    }
    assert.match(handler, /if \(shutdownRequested\) \{[\s\S]{0,120}return;/, 'a second signal does not start a second shutdown');
    assert.match(body, /leaveActiveMeeting = \(\) => meeting\.leaveMeeting\(page\);/);
});

test("main()'s own cleanup leaves the meeting before closing the browser, and stands aside after a stop signal", () => {
    const body = source('index.ts');
    const cleanup = body.slice(body.indexOf("    } finally {\n        // A stop signal has its own shutdown"));
    assert.match(cleanup, /^ {4}\} finally \{[\s\S]{0,600}if \(shutdownRequested\) \{[\s\S]{0,120}return;/);
    const leave = cleanup.indexOf('if (exitInfo && shouldLeaveAfterExit(exitInfo)) {');
    assert.ok(leave > 0);
    assert.ok(cleanup.indexOf('await closeAndPersistProfile();') > leave);
    assert.ok(cleanup.indexOf('meetingConsent.stop();') < leave, 'after the consent flow has its evidence');
    assert.match(body, /if \(statusManager && !shutdownRequested\) \{\s*\/\/ A thrown error carries no ExitInfo/);
});

test('the ECS entrypoint passes the stop signal to the app', () => {
    const script = readFileSync(new URL('../entrypoint.sh', import.meta.url), 'utf8');
    const start = script.slice(script.lastIndexOf('else\n'));
    assert.match(start, /node dist\/index\.js &\n\s*APP_PID=\$!/);
    assert.match(start, /trap 'kill -TERM "\$APP_PID" 2>\/dev\/null' TERM/);
    assert.match(start, /trap 'kill -INT "\$APP_PID" 2>\/dev\/null' INT/);
    assert.match(start, /while kill -0 "\$APP_PID" 2>\/dev\/null; do\s*wait "\$APP_PID"/);
});
