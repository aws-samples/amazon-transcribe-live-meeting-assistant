/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Evidence that the meeting the VP is in is still live: when a participant
 * last spoke.
 *
 * The attendee watchdogs (teams.ts decideAttendeeAction, also used by Google
 * Meet) end the meeting when the attendee count stays unreadable for long
 * enough, because a meeting that ends without an end screen leaves nothing
 * else to go on (#540). But some clients never show a readable count at all
 * (the Teams light-meetings client has no attendee badge), and there a bound on
 * an unreadable count ends every meeting at the same point however live it is
 * (#1148). Participants talking is evidence the bound cannot get from the page:
 * a meeting that has ended sends no participant audio. So while a participant
 * has spoken within PARTICIPANT_ACTIVITY_WINDOW_MS, an unreadable count is not
 * counted toward leaving, and the bound applies only to a meeting that is both
 * unreadable and silent.
 *
 * Sources: a final transcript on the participants' channel (scribe.ts; the
 * voice assistant's own channel is left out), and the platform's active
 * speaker changing to somebody other than the VP.
 */

/** How long after a participant last spoke the meeting still counts as live. */
export const PARTICIPANT_ACTIVITY_WINDOW_MS = 10 * 60 * 1000;

let lastActivityAt: number | null = null;
let lastActivitySource = '';

/** A participant (not the VP) spoke, or became the active speaker. */
export function noteParticipantActivity(source: string, now: number = Date.now()): void {
    lastActivityAt = now;
    lastActivitySource = source;
}

/** Whether a participant was active within `windowMs` of `now`. */
export function participantsActiveWithin(
    windowMs: number = PARTICIPANT_ACTIVITY_WINDOW_MS,
    now: number = Date.now(),
): boolean {
    return lastActivityAt !== null && now - lastActivityAt <= windowMs;
}

/** When, and from what, a participant was last active; for log lines. */
export function lastParticipantActivity(): { at: number | null; source: string } {
    return { at: lastActivityAt, source: lastActivitySource };
}

/** Forget all activity (tests). */
export function resetParticipantActivity(): void {
    lastActivityAt = null;
    lastActivitySource = '';
}
