/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The end signals that read the meeting page (#1148). Each "ends" test has a
 * matching "stays in a live meeting" test.
 */
import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import {
    decideHangupHidden,
    decideWebexEnd,
    decideZoomCounterAction,
    HANGUP_HIDDEN_CONFIRM_POLLS,
    WEBEX_END_TEXT_PERSIST_MS,
    webexEndReason,
    ZOOM_COUNTER_POLLS_BEFORE_ALONE,
    ZOOM_COUNTER_POLLS_BEFORE_REMOVED,
    zoomEndDialogReason,
    type HangupHiddenState,
    type WebexEndState,
    type ZoomCounterState,
} from './end-signals.js';

// --- Teams: hang-up button hidden ------------------------------------------

const GONE = { hangupVisible: false, inMeetingSignal: null };

test('Teams: the call UI gone for the confirmation checks ends the meeting', () => {
    const state: HangupHiddenState = { confirmations: 0 };
    for (let i = 1; i < HANGUP_HIDDEN_CONFIRM_POLLS; i += 1) {
        assert.equal(decideHangupHidden(GONE, state), 'confirming');
    }
    assert.equal(decideHangupHidden(GONE, state), 'end');
});

test('Teams: a hidden hang-up button with another in-meeting control visible does not end the meeting', () => {
    const state: HangupHiddenState = { confirmations: 0 };
    for (let i = 0; i < 100; i += 1) {
        assert.equal(decideHangupHidden({ hangupVisible: false, inMeetingSignal: '#chat-button' }, state), 'chrome-present');
    }
    assert.equal(state.confirmations, 0);
});

test('Teams: the button coming back, or a control reappearing, restarts the confirmation', () => {
    const state: HangupHiddenState = { confirmations: 0 };
    decideHangupHidden(GONE, state);
    decideHangupHidden(GONE, state);
    assert.equal(decideHangupHidden({ hangupVisible: true, inMeetingSignal: null }, state), 'visible');
    assert.equal(state.confirmations, 0);
    decideHangupHidden(GONE, state);
    decideHangupHidden(GONE, state);
    decideHangupHidden({ hangupVisible: false, inMeetingSignal: '[data-tid="calling-toolbar"]' }, state);
    assert.equal(decideHangupHidden(GONE, state), 'confirming', 'one unreadable check after a re-render is not an end');
});

// --- Zoom: participant counter ----------------------------------------------

const freshZoom = (): ZoomCounterState => ({ consecutiveMissing: 0, consecutiveLonely: 0 });

test('Zoom: the counter gone with no participant activity ends the meeting as removed', () => {
    for (const state of ['COUNTER_GONE', 'PAGE_DEAD'] as const) {
        const s = freshZoom();
        for (let i = 1; i < ZOOM_COUNTER_POLLS_BEFORE_REMOVED; i += 1) {
            assert.equal(decideZoomCounterAction({ state }, s, false), 'continue', `${state} poll ${i}`);
        }
        assert.equal(decideZoomCounterAction({ state }, s, false), 'end-removed');
    }
});

test('Zoom: the counter gone while participants are active does not end the meeting', () => {
    const s = freshZoom();
    for (let i = 0; i < 50; i += 1) {
        assert.equal(decideZoomCounterAction({ state: 'COUNTER_GONE' }, s, true), 'vetoed');
    }
    assert.equal(s.consecutiveMissing, 0);
});

test('Zoom: the removed bound is wider than the old two polls', () => {
    assert.ok(ZOOM_COUNTER_POLLS_BEFORE_REMOVED > 2);
    const s = freshZoom();
    decideZoomCounterAction({ state: 'COUNTER_GONE' }, s, false);
    assert.equal(decideZoomCounterAction({ state: 'COUNTER_GONE' }, s, false), 'continue');
});

test('Zoom: a readable count resets the missing run, and a sustained count of one ends as alone', () => {
    const s = freshZoom();
    for (let i = 1; i < ZOOM_COUNTER_POLLS_BEFORE_REMOVED; i += 1) decideZoomCounterAction({ state: 'COUNTER_GONE' }, s, false);
    assert.equal(decideZoomCounterAction({ state: 'OK', count: 4 }, s, false), 'continue');
    assert.equal(s.consecutiveMissing, 0);
    for (let i = 1; i < ZOOM_COUNTER_POLLS_BEFORE_ALONE; i += 1) {
        assert.equal(decideZoomCounterAction({ state: 'OK', count: 1 }, s, false), 'continue');
    }
    assert.equal(decideZoomCounterAction({ state: 'OK', count: 1 }, s, false), 'end-alone');
});

// --- Zoom: end dialog -------------------------------------------------------

test("Zoom: the end and removal dialogs' text is recognised", () => {
    assert.equal(zoomEndDialogReason('This meeting has been ended by host  OK'), 'host-ended');
    assert.equal(zoomEndDialogReason('The meeting has ended. OK'), 'host-ended');
    assert.equal(zoomEndDialogReason('You have been removed from the meeting by the host'), 'removed-from-meeting');
});

test('Zoom: a dialog that is not an end dialog does not end the meeting', () => {
    assert.equal(zoomEndDialogReason(''), null);
    assert.equal(zoomEndDialogReason('Recording has started. Got it'), null);
    assert.equal(zoomEndDialogReason('Allow this app to use your microphone? Allow'), null);
});

// --- Webex: end text --------------------------------------------------------

test('Webex: the end text with the in-meeting controls gone ends the meeting', () => {
    const s: WebexEndState = { phraseSince: null };
    assert.equal(decideWebexEnd({ phrase: 'meeting has ended', controlsVisible: false }, s, true, 0), 'meeting has ended');
    assert.equal(webexEndReason('meeting has ended'), 'host-ended');
    assert.equal(webexEndReason('you have left the meeting'), 'page-closed');
    assert.equal(webexEndReason('meeting disconnected'), 'page-closed');
});

test('Webex: the end text with controls still visible and participants active does not end the meeting', () => {
    const s: WebexEndState = { phraseSince: null };
    for (let t = 0; t <= 4 * WEBEX_END_TEXT_PERSIST_MS; t += 2_000) {
        assert.equal(decideWebexEnd({ phrase: 'meeting ended', controlsVisible: true }, s, true, t), null);
    }
});

test('Webex: the end text that stays with controls visible and a silent meeting ends it after the persist time', () => {
    const s: WebexEndState = { phraseSince: null };
    assert.equal(decideWebexEnd({ phrase: 'meeting ended', controlsVisible: true }, s, false, 1_000), null);
    assert.equal(decideWebexEnd({ phrase: 'meeting ended', controlsVisible: true }, s, false, WEBEX_END_TEXT_PERSIST_MS), null);
    assert.equal(
        decideWebexEnd({ phrase: 'meeting ended', controlsVisible: true }, s, false, 1_000 + WEBEX_END_TEXT_PERSIST_MS),
        'meeting ended',
    );
});

test('Webex: the end text going away resets the persist clock', () => {
    const s: WebexEndState = { phraseSince: null };
    decideWebexEnd({ phrase: 'meeting ended', controlsVisible: true }, s, false, 0);
    decideWebexEnd({ phrase: null, controlsVisible: true }, s, false, WEBEX_END_TEXT_PERSIST_MS - 1);
    assert.equal(s.phraseSince, null);
    assert.equal(decideWebexEnd({ phrase: 'meeting ended', controlsVisible: true }, s, false, WEBEX_END_TEXT_PERSIST_MS + 1), null);
});
