/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Whether the VP has been admitted to its meeting, for code that decides
 * differently before and after the join (the dialog watchdog). Set when the
 * VP reports JOINED or ACTIVE (status-manager.ts updateStatus), whether or not
 * that status write lands: the VP is in the meeting either way. A VP process
 * handles one meeting, so nothing clears it except the tests.
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
