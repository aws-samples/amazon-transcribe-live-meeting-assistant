/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Who the meeting platform showed as active speaker at each point of the engine's
 * audio clock, so a row is attributed by when it STARTED rather than by whoever is
 * active when it is emitted. The VP's own identities are never recorded.
 */
export class RosterTimeline {
    private readonly own: Set<string>;
    private readonly times: number[] = [];
    private readonly names: string[] = [];

    constructor(ownIdentities: string[]) {
        this.own = new Set(ownIdentities.map((name) => name.trim()).filter((name) => name.length > 0));
    }

    record(atSeconds: number, name: string): void {
        const trimmed = name.trim();
        if (trimmed.length === 0 || trimmed === 'none' || this.own.has(trimmed)) {
            return;
        }
        const last = this.times.length > 0 ? this.times[this.times.length - 1] : -Infinity;
        this.times.push(Math.max(atSeconds, last));
        this.names.push(trimmed);
    }

    speakerAt(atSeconds: number): string {
        let lo = 0;
        let hi = this.times.length;
        while (lo < hi) {
            const mid = (lo + hi) >> 1;
            if (this.times[mid] <= atSeconds) {
                lo = mid + 1;
            } else {
                hi = mid;
            }
        }
        return lo === 0 ? 'Unknown' : this.names[lo - 1];
    }

    /** True until a participant has been recorded. */
    get isEmpty(): boolean {
        return this.times.length === 0;
    }

    /**
     * The participant shown for most of [startSec, endSec], with every change
     * moved back by `lagSeconds` (how late the platform reports a change). A
     * span no change overlaps -- a zero-length one, or one before the first
     * entry -- takes the participant in effect at its start, or the first one.
     *
     * Called for every word of every Amazon Transcribe result, partials
     * included, so it searches rather than scanning or copying the timeline.
     */
    speakerFor(startSec: number, endSec: number, lagSeconds = 0): string {
        const count = this.times.length;
        if (count === 0) {
            return 'Unknown';
        }
        const end = Math.max(endSec, startSec);
        const shifted = (i: number): number => this.times[i] - lagSeconds;
        // Entries up to `inEffect` start at or before the span; only the last
        // of them can overlap it.
        let lo = 0;
        let hi = count;
        while (lo < hi) {
            const mid = (lo + hi) >> 1;
            if (shifted(mid) <= startSec) {
                lo = mid + 1;
            } else {
                hi = mid;
            }
        }
        const inEffect = lo - 1;
        const covered = new Map<string, number>();
        for (let i = Math.max(inEffect, 0); i < count && shifted(i) < end; i += 1) {
            const from = Math.max(shifted(i), startSec);
            const to = Math.min(i + 1 < count ? shifted(i + 1) : Infinity, end);
            if (to > from) {
                covered.set(this.names[i], (covered.get(this.names[i]) ?? 0) + (to - from));
            }
        }
        let best = '';
        let bestCovered = 0;
        for (const [name, seconds] of covered) {
            if (seconds >= bestCovered) {
                best = name;
                bestCovered = seconds;
            }
        }
        if (best) {
            return best;
        }
        return inEffect < 0 ? this.names[0] : this.names[inEffect];
    }
}
