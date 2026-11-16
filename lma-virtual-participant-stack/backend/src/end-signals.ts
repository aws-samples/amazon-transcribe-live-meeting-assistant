/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The decisions behind the platform end signals that read the meeting page
 * rather than a count (#1148): Teams' hang-up button being hidden, Zoom's
 * participant counter going away, Zoom's end dialog, and Webex's end text.
 *
 * Each of these used to end the meeting on one observation that a live meeting
 * can also produce: a toolbar that hides itself or re-renders, a primary button
 * that is not part of any dialog, or the words "meeting ended" in a chat
 * message. Each decision here keeps the real end it was written for and asks for
 * corroboration before treating the observation as one. They are pure so the
 * rules can be unit-tested; the platform handlers do the page reads.
 */

// ---------------------------------------------------------------------------
// Teams: the hang-up button is hidden
// ---------------------------------------------------------------------------

/** How often the hidden hang-up button is re-checked while it is confirmed. */
export const HANGUP_HIDDEN_POLL_MS = 5_000;

/**
 * Consecutive checks (HANGUP_HIDDEN_POLL_MS apart) in which the hang-up button
 * is hidden and no other in-meeting control is visible before the meeting
 * counts as ended: about 15 seconds.
 */
export const HANGUP_HIDDEN_CONFIRM_POLLS = 3;

export interface HangupHiddenReading {
    /** Whether `#hangup-button` is visible again. */
    hangupVisible: boolean;
    /** The in-meeting control that is still visible (teams.ts inMeetingSignal), or null. */
    inMeetingSignal: string | null;
}

export interface HangupHiddenState {
    /** Consecutive checks with the button hidden and no in-meeting control. */
    confirmations: number;
}

/**
 * - `visible`: the button is back; go back to waiting for it to be hidden.
 * - `chrome-present`: the button is hidden but another in-meeting control is
 *   visible, so the meeting is still on screen (a collapsed toolbar, a
 *   re-render). Keep checking; the attendee watchdog covers a meeting that has
 *   ended with its controls left behind (#540).
 * - `confirming`: nothing in-meeting is visible, not yet for long enough.
 * - `end`: nothing in-meeting has been visible for HANGUP_HIDDEN_CONFIRM_POLLS checks.
 */
export type HangupHiddenAction = 'visible' | 'chrome-present' | 'confirming' | 'end';

export function decideHangupHidden(
    reading: HangupHiddenReading,
    state: HangupHiddenState,
    confirmPolls: number = HANGUP_HIDDEN_CONFIRM_POLLS,
): HangupHiddenAction {
    if (reading.hangupVisible) {
        state.confirmations = 0;
        return 'visible';
    }
    if (reading.inMeetingSignal !== null) {
        state.confirmations = 0;
        return 'chrome-present';
    }
    state.confirmations += 1;
    return state.confirmations >= confirmPolls ? 'end' : 'confirming';
}

// ---------------------------------------------------------------------------
// Zoom: the participant counter
// ---------------------------------------------------------------------------

/** Cadence of the Zoom participants watchdog. */
export const ZOOM_COUNTER_POLL_MS = 30_000;

/** Consecutive readings of one or fewer attendees before the VP leaves as alone. */
export const ZOOM_COUNTER_POLLS_BEFORE_ALONE = 2;

/**
 * Consecutive polls with the counter gone (or the page unreadable) before the
 * VP counts as removed, while no participant has been active within the
 * liveness window: about 2 minutes. While participants are active the counter
 * being gone does not count at all.
 */
export const ZOOM_COUNTER_POLLS_BEFORE_REMOVED = 4;

export type ZoomCounterReading =
    | { state: 'OK'; count: number }
    | { state: 'COUNTER_GONE' }
    | { state: 'PAGE_DEAD' };

export interface ZoomCounterState {
    consecutiveMissing: number;
    consecutiveLonely: number;
}

/**
 * - `continue`: nothing to act on.
 * - `vetoed`: the counter is gone, but participants have been active within
 *   the liveness window, so the meeting is live and the reading is not counted.
 * - `end-removed`: the counter has been gone for ZOOM_COUNTER_POLLS_BEFORE_REMOVED
 *   polls with no participant activity.
 * - `end-alone`: one or fewer attendees for ZOOM_COUNTER_POLLS_BEFORE_ALONE polls.
 */
export type ZoomCounterAction = 'continue' | 'vetoed' | 'end-removed' | 'end-alone';

export function decideZoomCounterAction(
    reading: ZoomCounterReading,
    state: ZoomCounterState,
    participantsActive: boolean,
): ZoomCounterAction {
    if (reading.state !== 'OK') {
        state.consecutiveLonely = 0;
        if (participantsActive) {
            state.consecutiveMissing = 0;
            return 'vetoed';
        }
        state.consecutiveMissing += 1;
        return state.consecutiveMissing >= ZOOM_COUNTER_POLLS_BEFORE_REMOVED ? 'end-removed' : 'continue';
    }
    state.consecutiveMissing = 0;
    if (reading.count <= 1) {
        state.consecutiveLonely += 1;
        return state.consecutiveLonely >= ZOOM_COUNTER_POLLS_BEFORE_ALONE ? 'end-alone' : 'continue';
    }
    state.consecutiveLonely = 0;
    return 'continue';
}

// ---------------------------------------------------------------------------
// Zoom: the end dialog
// ---------------------------------------------------------------------------

/** Text of Zoom's "the meeting has ended" and "you were removed" dialogs (lower case). */
export const ZOOM_END_DIALOG_PHRASES: readonly string[] = [
    'meeting has been ended',
    'meeting has ended',
    'meeting is end',
    'removed from the meeting',
    'have been removed',
];

/**
 * Why the meeting ended, from the text of a Zoom dialog, or null when the
 * dialog is not one of Zoom's end dialogs. The page-side check in zoom.ts uses
 * the same phrases, on the text of a dialog that holds Zoom's primary button.
 */
export function zoomEndDialogReason(dialogText: string): 'removed-from-meeting' | 'host-ended' | null {
    const text = dialogText.toLowerCase();
    if (!ZOOM_END_DIALOG_PHRASES.some((p) => text.includes(p))) return null;
    return text.includes('removed') ? 'removed-from-meeting' : 'host-ended';
}

// ---------------------------------------------------------------------------
// Webex: the end text
// ---------------------------------------------------------------------------

/** Text of Webex's end screens (lower case). */
export const WEBEX_END_PHRASES: readonly string[] = [
    'meeting has ended',
    'this meeting has ended',
    'meeting ended',
    'you have left the meeting',
    'meeting disconnected',
];

/**
 * Where meeting participants' words appear in the Webex page: chat messages,
 * captions and transcripts. Text inside these is not read for the end phrases.
 */
export const WEBEX_PARTICIPANT_TEXT_SELECTORS: readonly string[] = [
    '#activity-list',
    'div[class^="style-chat-box"]',
    '.activity-item',
    '[class*="caption" i]',
    '[class*="transcript" i]',
];

/**
 * In-meeting controls of the Webex client. While one is visible, the meeting
 * is still on screen.
 */
export const WEBEX_IN_MEETING_CONTROLS: readonly string[] = [
    'mdc-button[data-test="microphone-button"]',
    'mdc-button[data-test="camera-button"]',
    '#audioControlButton',
    'button[data-doi*="MUTE"]',
    'button[data-test="participants-toggle-button"]',
    'button[aria-label*="mute" i]',
    'button[aria-label*="leave" i]',
];

/** How often webex.ts reads the page for the end text. */
export const WEBEX_END_POLL_MS = 2_000;

/**
 * How long the end text must stay on the page, with in-meeting controls still
 * visible and no participant active within the liveness window, before the
 * meeting counts as ended anyway: the backstop for an end screen that leaves a
 * control behind.
 */
export const WEBEX_END_TEXT_PERSIST_MS = 2 * 60_000;

export interface WebexEndReading {
    /** The end phrase found outside chat, captions and transcripts, or null. */
    phrase: string | null;
    /** Whether an in-meeting control is visible. */
    controlsVisible: boolean;
}

export interface WebexEndState {
    /** When the end phrase was first seen with controls still visible. */
    phraseSince: number | null;
}

export function decideWebexEnd(
    reading: WebexEndReading,
    state: WebexEndState,
    participantsActive: boolean,
    now: number = Date.now(),
): string | null {
    if (reading.phrase === null) {
        state.phraseSince = null;
        return null;
    }
    if (!reading.controlsVisible) return reading.phrase;
    if (state.phraseSince === null) state.phraseSince = now;
    if (!participantsActive && now - state.phraseSince >= WEBEX_END_TEXT_PERSIST_MS) return reading.phrase;
    return null;
}

/** Why the meeting ended, from the Webex end phrase that matched. */
export function webexEndReason(phrase: string): 'page-closed' | 'host-ended' {
    const text = phrase.toLowerCase();
    return text.includes('disconnected') || text.includes('left the meeting') ? 'page-closed' : 'host-ended';
}
