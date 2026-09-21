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
}
