/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Whether the VP has been admitted to its meeting, for code that decides
 * differently before and after the join (the dialog watchdog). Set at the
 * first of: the handler's own admission check (zoom.ts and teams.ts, right
 * after the VP is let in); the handler attaching the consent flow
 * (consent-runtime.ts attachMeetingConsent, which every handler calls, consent
 * or not, once it is reading the chat, so it covers the handlers without a
 * separate admission check); and the VP reporting JOINED or ACTIVE
 * (status-manager.ts updateStatus), whether or not that status write lands.
 *
 * A VP process handles one meeting, so nothing clears it except the tests. A
 * VP that a platform sends back to its lobby after admission therefore keeps
 * treating unclassified dialogs as in-meeting, on purpose: it has already been
 * admitted once, and the handlers' own lobby and removal detection decide what
 * happens next, not the dialog watchdog.
 */

let joined = false;

/** The VP has been admitted to the meeting. */
export function noteMeetingJoined(): void {
    joined = true;
}

/** Whether the VP has been admitted to the meeting. */
export function meetingJoined(): boolean {
    return joined;
}

/** Forget the join (tests). */
export function resetMeetingPhase(): void {
    joined = false;
}
