/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Tests for the dialog watchdog's click / escalate / ignore rules (GitHub #775).
 *
 * The watchdog acts on the dialog type, not on the model's needsHuman flag, and
 * never clicks an action that would join a meeting without audio.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import type { DialogAnalysis } from './ai-dom-resolver.js';
import {
    decideAfterClickFailure,
    decideDialogAction,
    isMediaPermissionDialog,
    isNoAudioAction,
    manualActionMessage,
    meetingOverReason,
    NO_AUDIO_WARNING,
} from './dialog-decision.js';

const consent = (overrides: Partial<DialogAnalysis> = {}): DialogAnalysis => ({
    type: 'CONSENT',
    primaryActionSelector: 'button[data-tid="allow"]',
    message: 'Teams is asking to use your camera and microphone.',
    needsHuman: false,
    mediaPermission: true,
    ...overrides,
});

test('clicks a CONSENT action even when the model set needsHuman', () => {
    const d = decideDialogAction(consent({ needsHuman: true }), 'TEAMS', 'Allow');
    assert.deepEqual(d, { kind: 'click', selector: 'button[data-tid="allow"]' });
});

test('clicks a RECORDING_NOTICE action', () => {
    const d = decideDialogAction(
        {
            type: 'RECORDING_NOTICE',
            primaryActionSelector: '#got-it',
            message: 'This meeting is being recorded.',
            needsHuman: false,
        },
        'ZOOM',
        'Got it',
    );
    assert.deepEqual(d, { kind: 'click', selector: '#got-it' });
});

test('clicks an icon-only action on a dialog that is not about media devices', () => {
    const notice: DialogAnalysis = {
        type: 'RECORDING_NOTICE',
        primaryActionSelector: 'button[aria-label]',
        message: 'This meeting is being recorded.',
        needsHuman: false,
    };
    assert.equal(decideDialogAction(notice, 'ZOOM', '').kind, 'click');
});

test('escalates an icon-only action on a camera/microphone dialog', () => {
    const d = decideDialogAction(consent(), 'TEAMS', '');
    assert.ok(d.kind === 'escalate' && d.message.includes(NO_AUDIO_WARNING));
});

test('escalates a camera/microphone dialog that has no action to click', () => {
    const d = decideDialogAction(consent({ primaryActionSelector: undefined }), 'TEAMS', null);
    assert.equal(d.kind, 'escalate');
});

test('escalates a camera/microphone dialog whose action cannot be found or read', () => {
    assert.equal(decideDialogAction(consent(), 'TEAMS', null).kind, 'escalate');
});

const recordingNotice = (overrides: Partial<DialogAnalysis> = {}): DialogAnalysis => ({
    type: 'RECORDING_NOTICE',
    primaryActionSelector: '#got-it',
    message: 'This meeting is being recorded.',
    needsHuman: false,
    ...overrides,
});

test('leaves a recording notice alone when it has no action to click', () => {
    const d = decideDialogAction(recordingNotice({ primaryActionSelector: undefined }), 'TEAMS', null);
    assert.deepEqual(d, { kind: 'ignore' });
});

test('leaves a recording notice alone when its action cannot be found or read', () => {
    assert.deepEqual(decideDialogAction(recordingNotice(), 'TEAMS', null), { kind: 'ignore' });
});

test('escalates a notice without a usable action only when the model says a human is needed', () => {
    const notice = recordingNotice({ primaryActionSelector: undefined, needsHuman: true });
    const d = decideDialogAction(notice, 'TEAMS', null);
    assert.deepEqual(d, { kind: 'escalate', message: 'This meeting is being recorded.' });
});

test('leaves alone a notice that mentions the microphone when the model says it is not a media dialog', () => {
    const notice = recordingNotice({
        message: 'This meeting is being recorded, including your microphone audio.',
        mediaPermission: false,
    });
    assert.deepEqual(decideDialogAction(notice, 'TEAMS', null), { kind: 'ignore' });
    assert.deepEqual(decideAfterClickFailure(notice, 'TEAMS', 'Got it'), { kind: 'ignore' });
    assert.equal(decideDialogAction(notice, 'TEAMS', '').kind, 'click');
});

test('a failed click escalates a camera/microphone dialog', () => {
    const d = decideAfterClickFailure(consent(), 'TEAMS', 'Allow');
    assert.ok(d.kind === 'escalate' && d.message.includes(NO_AUDIO_WARNING));
});

test('a failed click leaves any other dialog alone', () => {
    assert.deepEqual(decideAfterClickFailure(recordingNotice(), 'ZOOM', 'Got it'), { kind: 'ignore' });
});

test('never clicks an action that continues without audio', () => {
    const d = decideDialogAction(consent(), 'TEAMS', 'Continue without audio or video');
    assert.equal(d.kind, 'escalate');
    assert.ok(d.kind === 'escalate' && d.message.endsWith(NO_AUDIO_WARNING));
});

test('escalation message for a media dialog carries the no-audio warning', () => {
    const d = decideDialogAction(consent({ primaryActionSelector: undefined }), 'TEAMS', null);
    assert.ok(d.kind === 'escalate' && d.message.includes(NO_AUDIO_WARNING));
});

test('escalates CAPTCHA and leaves an unclassified dialog alone', () => {
    const captcha = decideDialogAction(
        { type: 'CAPTCHA', primaryActionSelector: '#verify', message: 'Solve the puzzle.', needsHuman: true },
        'ZOOM',
        'Verify',
    );
    assert.deepEqual(captcha, { kind: 'escalate', message: 'Solve the puzzle.' });
    const other = decideDialogAction(
        { type: 'OTHER', message: 'A tip about new features.', needsHuman: false },
        'TEAMS',
        null,
    );
    assert.deepEqual(other, { kind: 'ignore' });
});

test('recognises no-audio action labels', () => {
    for (const label of [
        'Continue without audio or video',
        'Join without audio',
        'Join without computer audio',
        "Don't use audio",
        'Do not use audio',
        'No audio',
    ]) {
        assert.ok(isNoAudioAction(label), label);
    }
    for (const label of ['Allow', 'Join now', 'Use computer audio', 'Join with computer audio', 'Continue', '', null]) {
        assert.ok(!isNoAudioAction(label), String(label));
    }
});

test('identifies a media-permission dialog from the flag, the action, or (without a flag) the message', () => {
    assert.ok(isMediaPermissionDialog({ message: '', mediaPermission: true }));
    assert.ok(isMediaPermissionDialog({ message: 'Allow access to your microphone?' }));
    assert.ok(!isMediaPermissionDialog({ message: 'Allow access to your microphone?', mediaPermission: false }));
    assert.ok(isMediaPermissionDialog({ message: '', mediaPermission: false }, 'Continue without audio or video'));
    assert.ok(isMediaPermissionDialog({ message: 'Before you join' }, 'Continue without audio or video'));
    assert.ok(!isMediaPermissionDialog({ message: 'This meeting is being recorded.' }, 'Got it'));
});

test('manual action message falls back to a platform message', () => {
    assert.equal(
        manualActionMessage({ message: '' }, 'WEBEX'),
        'Manual action required in the WEBEX web client.',
    );
});

// ─── once the VP is in the meeting ────────────────────────────────────────

const unclassified = (needsHuman: boolean): DialogAnalysis => ({
    type: 'OTHER',
    message: 'Whiteboard dashboard is loading in a modal window during meeting join.',
    needsHuman,
});

test('an unclassified dialog the model says needs a human goes to one before the join', () => {
    assert.deepEqual(decideDialogAction(unclassified(true), 'ZOOM', null), {
        kind: 'escalate',
        message: 'Whiteboard dashboard is loading in a modal window during meeting join.',
    });
    assert.deepEqual(decideDialogAction(unclassified(true), 'ZOOM', null, false), {
        kind: 'escalate',
        message: 'Whiteboard dashboard is loading in a modal window during meeting join.',
    });
});

test('once the VP is in the meeting an unclassified dialog never marks it as needing manual action', () => {
    assert.deepEqual(decideDialogAction(unclassified(true), 'ZOOM', null, true), { kind: 'ignore' });
    assert.deepEqual(decideDialogAction(unclassified(false), 'ZOOM', null, true), { kind: 'ignore' });
});

test('a CAPTCHA or sign-in dialog still goes to a human in the meeting', () => {
    for (const type of ['CAPTCHA', 'LOGIN_REQUIRED', 'SSO_REDIRECT', 'BLOCKED'] as const) {
        const d = decideDialogAction({ type, message: `${type} shown`, needsHuman: true }, 'ZOOM', null, true);
        assert.equal(d.kind, 'escalate', type);
    }
});

test('in the meeting, a consent or recording notice is still clicked', () => {
    const d = decideDialogAction(
        { type: 'RECORDING_NOTICE', primaryActionSelector: '#ok', message: 'This meeting is being recorded.', needsHuman: false },
        'ZOOM',
        'Got it',
        true,
    );
    assert.deepEqual(d, { kind: 'click', selector: '#ok' });
});

// ── the end of the meeting is an exit, not a manual action ───────────────────

const removed = (overrides: Partial<DialogAnalysis> = {}): DialogAnalysis => ({
    type: 'BLOCKED',
    message: 'User has been removed from the meeting by the host',
    needsHuman: true,
    ...overrides,
});

test('a dialog saying the VP was removed by the host ends the meeting as a removal, never as a manual action', () => {
    for (const inMeeting of [true, false]) {
        assert.deepEqual(decideDialogAction(removed(), 'ZOOM', null, inMeeting), {
            kind: 'meeting-over',
            reason: 'removed-from-meeting',
        });
    }
    assert.deepEqual(decideDialogAction(removed({ type: 'OTHER', message: 'The host removed you from this meeting.' }), 'ZOOM', 'OK', true), {
        kind: 'meeting-over',
        reason: 'removed-from-meeting',
    });
});

test('a dialog saying the meeting has ended ends it as ended by the host, never as a manual action', () => {
    for (const message of ['This meeting has been ended by host', 'The host has ended this meeting', 'The meeting has ended']) {
        assert.deepEqual(decideDialogAction(removed({ message }), 'ZOOM', null, true), { kind: 'meeting-over', reason: 'host-ended' }, message);
    }
});

test('a blocking dialog that is not an end notice still goes to a human', () => {
    const d = decideDialogAction(removed({ message: 'The browser was blocked by bot detection' }), 'ZOOM', null, true);
    assert.equal(d.kind, 'escalate');
    assert.equal(meetingOverReason({ type: 'CAPTCHA', message: 'removed from the meeting' }), null, 'a CAPTCHA is never read as an end notice');
});

test('no dialog is escalated or clicked once the meeting is over for the VP', () => {
    const blocking = removed({ type: 'CAPTCHA', message: 'Solve the puzzle to continue' });
    assert.deepEqual(decideDialogAction(blocking, 'ZOOM', null, true, true), { kind: 'ignore' });
    assert.deepEqual(decideDialogAction(removed(), 'ZOOM', null, true, true), { kind: 'ignore' });
    assert.deepEqual(decideDialogAction(consent(), 'TEAMS', 'Allow', true, true), { kind: 'ignore' });
});

test('an in-meeting notice about something else ending, or someone else removed, does not end the VP’s meeting', () => {
    for (const message of [
        'The host has ended screen sharing',
        'The host has ended the poll',
        'Breakout rooms have ended',
        'The host ended the breakout rooms',
        'Alice has been removed from the meeting',
        'A participant was removed from the meeting by the host',
    ]) {
        for (const type of ['OTHER', 'BLOCKED'] as const) {
            assert.equal(meetingOverReason({ type, message }), null, `${type}: ${message}`);
            assert.notEqual(decideDialogAction(removed({ type, message, needsHuman: false }), 'ZOOM', null, true).kind, 'meeting-over', message);
        }
    }
});

test('before the join a removal notice is neither escalated nor handed to the handler', () => {
    assert.equal(decideDialogAction(removed(), 'ZOOM', null, false).kind, 'meeting-over');
    // The handler is told only after the join: the join paths have their own
    // error-screen detection.
    const watchdog = readFileSync(new URL('../src/dialog-watchdog.ts', import.meta.url), 'utf8');
    assert.match(watchdog, /if \(meetingJoined\(\)\) opts\.onMeetingOver\?\.\(decision\.reason\)/);
});
