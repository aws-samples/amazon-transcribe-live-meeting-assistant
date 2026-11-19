/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The wait for the first other attendee of a meeting the VP joined first.
 *
 * Every platform's "everyone else has left" check ends the meeting once the
 * attendee count has stayed at one or fewer for a short time (alone-debounce.ts,
 * end-signals.ts, the Teams and Meet watchdog, the SDK pollers). A VP that joins
 * before anybody else reads one attendee, itself, from the start, so those
 * checks used to end a meeting nobody had arrived at yet, about a minute after
 * the VP was admitted.
 *
 * Until a reading above one has been seen, a count of one or fewer now ends the
 * meeting only once FIRST_ATTENDEE_WAIT_MS has passed since the VP joined. Once
 * somebody else has been in the meeting, the checks behave as before. A VP still
 * alone when the wait is over leaves then, so one sent to a meeting that never
 * happens does not stay until its maximum duration.
 */

/** How long a VP that joined first waits for anybody else before leaving as alone. */
export const FIRST_ATTENDEE_WAIT_MS = 5 * 60_000;

export class FirstAttendeeWait {
    private seenOthers = false;
    private readonly joinedAt: number;

    constructor(
        private readonly waitMs: number = FIRST_ATTENDEE_WAIT_MS,
        private readonly now: () => number = Date.now,
    ) {
        this.joinedAt = now();
    }

    /** One attendee-count reading, the VP included. Not a number: ignored. */
    note(count: number): void {
        if (Number.isFinite(count) && count > 1) this.seenOthers = true;
    }

    /** Whether anybody other than the VP has been counted in the meeting. */
    get othersSeen(): boolean {
        return this.seenOthers;
    }

    /** Milliseconds until a count of one or fewer may end the meeting; 0 when it may now. */
    remainingMs(): number {
        if (this.seenOthers) return 0;
        return Math.max(0, this.joinedAt + this.waitMs - this.now());
    }

    /** Whether a count of one or fewer may end the meeting now. */
    mayLeaveAlone(): boolean {
        return this.remainingMs() === 0;
    }
}
