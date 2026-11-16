/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * "Everyone else has left", decided from a sustained count rather than one
 * reading (#1148).
 *
 * Zoom's and Chime's attendee observers report every change of the attendee
 * counter, and they used to leave on the first reading of one or fewer. A
 * counter that re-renders, or reads empty for a moment while the panel
 * redraws, then ends a meeting that is still running. The VP now leaves only
 * when the count has stayed at one or fewer (the VP alone) for
 * ALONE_HOLD_MS; any reading above one in that time cancels it. That is a
 * little longer than the Teams watchdog's default (3 checks 20 s apart, about
 * 40 s from the first low reading).
 */

/** How long the count must stay at one or fewer before the VP leaves. */
export const ALONE_HOLD_MS = 60_000;

export class AloneDebounce {
    private timer: ReturnType<typeof setTimeout> | null = null;

    constructor(
        private readonly onAlone: () => void,
        private readonly holdMs: number = ALONE_HOLD_MS,
    ) {}

    /** One attendee-count reading, the VP included. */
    note(count: number): void {
        if (!Number.isFinite(count)) return;
        if (count > 1) {
            this.cancel();
            return;
        }
        if (this.timer) return;
        this.timer = setTimeout(() => {
            this.timer = null;
            this.onAlone();
        }, this.holdMs);
    }

    /** Whether a reading of one or fewer is waiting out the hold. */
    get pending(): boolean {
        return this.timer !== null;
    }

    cancel(): void {
        if (this.timer) clearTimeout(this.timer);
        this.timer = null;
    }
}
