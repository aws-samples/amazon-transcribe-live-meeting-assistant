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
import type { DialogAnalysis } from './ai-dom-resolver.js';
import {
    decideAfterClickFailure,
    decideDialogAction,
    isMediaPermissionDialog,
    isNoAudioAction,
    manualActionMessage,
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

test('identifies a media-permission dialog from the flag, the message, or the action', () => {
    assert.ok(isMediaPermissionDialog({ message: '', mediaPermission: true }));
    assert.ok(isMediaPermissionDialog({ message: 'Allow access to your microphone?' }));
    assert.ok(isMediaPermissionDialog({ message: 'Before you join' }, 'Continue without audio or video'));
    assert.ok(!isMediaPermissionDialog({ message: 'This meeting is being recorded.' }, 'Got it'));
});

test('manual action message falls back to a platform message', () => {
    assert.equal(
        manualActionMessage({ message: '' }, 'WEBEX'),
        'Manual action required in the WEBEX web client.',
    );
});
