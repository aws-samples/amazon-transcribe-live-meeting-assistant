/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Who the meeting platform showed as active speaker at each point of the
 * transcription audio clock (the on-demand engine's, or the audio sent to Amazon
 * Transcribe), so a row or a word is attributed to whoever was shown for most of
 * it rather than to whoever is active when it is emitted. The VP's own identities
 * are never recorded.
 */
const VOICE_LABEL = /\s*\(spk_\d+\)$/;

export class RosterTimeline {
    private readonly own: Set<string>;
    private readonly times: number[] = [];
    private readonly names: string[] = [];
    // The platform's own id for each entry's participant, where the platform
    // gives one with the active speaker ('' otherwise); see platformIdFor.
    private readonly ids: string[] = [];

    constructor(ownIdentities: string[]) {
        this.own = new Set(ownIdentities.map((name) => name.trim()).filter((name) => name.length > 0));
    }

    /**
     * `platformId` is the platform's own id for the participant, read from the
     * same active-speaker report as the name; empty or absent where the
     * platform gives none.
     */
    record(atSeconds: number, name: string, platformId?: string | null): void {
        const trimmed = name.trim();
        if (trimmed.length === 0 || trimmed === 'none' || this.own.has(trimmed)) {
            return;
        }
        const last = this.times.length > 0 ? this.times[this.times.length - 1] : -Infinity;
        this.times.push(Math.max(atSeconds, last));
        this.names.push(trimmed);
        this.ids.push(typeof platformId === 'string' ? platformId.trim() : '');
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
        if (this.times.length === 0) {
            return 'Unknown';
        }
        const { covered, inEffect } = this.cover(startSec, endSec, lagSeconds);
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

    /**
     * Seconds of [startSec, endSec] each participant was shown for, with the
     * same lag as speakerFor. Time before the first entry belongs to nobody,
     * so it is absent rather than credited to the first participant: this is
     * the evidence a voice label's name vote is built from (speaker-fusion.ts),
     * and a guess must not count as evidence.
     */
    coverage(startSec: number, endSec: number, lagSeconds = 0): Map<string, number> {
        return this.times.length === 0 ? new Map() : this.cover(startSec, endSec, lagSeconds).covered;
    }

    /**
     * The platform id of the participant shown for most of [startSec,
     * endSec], with the same lag as speakerFor, when that participant is
     * `name` (a row's speaker as stored; the speech engine's voice label is
     * ignored). Undefined when the platform gave no id for them, when somebody
     * else was shown for most of the span, or when the span is before anyone
     * was shown: the id goes on a row only where the platform's report that
     * names the row also gives the id.
     *
     * Entries are told apart by name and id together, so two participants
     * with one display name are never taken for one.
     */
    platformIdFor(startSec: number, endSec: number, name: string, lagSeconds = 0): string | undefined {
        const wanted = name.replace(VOICE_LABEL, '').trim();
        if (this.times.length === 0 || !wanted) return undefined;
        const { covered, inEffect } = this.cover(startSec, endSec, lagSeconds, (i) => `${this.ids[i]}\u0000${this.names[i]}`);
        let best: string | null = null;
        let bestCovered = 0;
        for (const [key, seconds] of covered) {
            if (seconds >= bestCovered) {
                best = key;
                bestCovered = seconds;
            }
        }
        if (best === null) {
            if (inEffect < 0) return undefined;
            best = `${this.ids[inEffect]}\u0000${this.names[inEffect]}`;
        }
        const [id, shown] = best.split('\u0000');
        return shown === wanted && id ? id : undefined;
    }

    private cover(
        startSec: number,
        endSec: number,
        lagSeconds: number,
        keyOf: (index: number) => string = (index) => this.names[index],
    ): { covered: Map<string, number>; inEffect: number } {
        const count = this.times.length;
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
                const key = keyOf(i);
                covered.set(key, (covered.get(key) ?? 0) + (to - from));
            }
        }
        return { covered, inEffect };
    }
}
