/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Which name a live (partial) row shows while it grows.
 *
 * Every partial of a row is named afresh by SpeakerFusion: by its voice label
 * when the engine sends one and the label is proven to a name, otherwise by the
 * meeting page's active-speaker signal. Taken one partial at a time that name
 * can flicker, because each partial is a new reading of a row that is still
 * being spoken: the page holds and flickers on short sounds, and a provisional
 * voice label is read from the last second of audio, where a backchannel or a
 * diarizer blip can briefly win.
 *
 * So a row keeps the name it shows until a different name is confirmed:
 *
 *  - A stronger source replaces a weaker one at once. A voice-proven name
 *    replaces a page name on the next partial, because a proven voice was the
 *    better signal on the meeting the fusion was tuned on, and showing it early
 *    is the point. An enrolled name (a consented voice) is stronger still.
 *  - Any other change -- a different name from the same or a weaker source --
 *    needs `confirmPartials` partials in a row agreeing on it. One stray reading
 *    therefore never renames the row, and a row cannot go A, B, A on single
 *    readings.
 *
 * The row's final is named by SpeakerFusion directly, not through this hold:
 * the final carries the engine's settled label, and is what is stored.
 */
import type { Attribution, FusionSource } from './speaker-fusion.js';

const RANK: Record<FusionSource, number> = { page: 0, voice: 1, enrolled: 2 };

/** Open rows remembered; a row is forgotten when its final arrives. */
const OPEN_ROWS = 64;

type HeldRow = {
    shown: Attribution;
    /** A different name seen on the latest partials, and how many in a row. */
    pending?: { attribution: Attribution; count: number };
};

/**
 * Two partials (about a second apart on the on-demand engine) was the shortest
 * confirmation that kept the number of name changes on live rows at the page
 * signal's own level in a replay of a 33-minute, three-person Zoom meeting;
 * with no confirmation the same rule changed names 50 times against the
 * page's 31, and returned to an abandoned name 18 times against 3.
 */
export const DEFAULT_CONFIRM_PARTIALS = 2;

export class PartialNameHold {
    private readonly rows = new Map<string, HeldRow>();

    constructor(private readonly confirmPartials: number = DEFAULT_CONFIRM_PARTIALS) {}

    /** The name a partial of `rowId` shows, given the fusion's name for it. */
    hold(rowId: string, candidate: Attribution): Attribution {
        const row = this.rows.get(rowId);
        if (row === undefined) {
            this.remember(rowId, { shown: candidate });
            return candidate;
        }
        if (candidate.name === row.shown.name) {
            row.pending = undefined;
            // Same person: take the stronger source's speaker string (it may
            // add the voice id), never a weaker one.
            if (RANK[candidate.source] >= RANK[row.shown.source]) {
                row.shown = candidate;
            }
            return row.shown;
        }
        if (RANK[candidate.source] > RANK[row.shown.source]) {
            row.shown = candidate;
            row.pending = undefined;
            return row.shown;
        }
        const count = row.pending?.attribution.name === candidate.name ? row.pending.count + 1 : 1;
        if (count >= this.confirmPartials) {
            row.shown = candidate;
            row.pending = undefined;
        } else {
            row.pending = { attribution: candidate, count };
        }
        return row.shown;
    }

    /** The row's final arrived: forget it, returning the name its partials last showed. */
    close(rowId: string): Attribution | undefined {
        const row = this.rows.get(rowId);
        this.rows.delete(rowId);
        return row?.shown;
    }

    private remember(rowId: string, row: HeldRow): void {
        this.rows.set(rowId, row);
        if (this.rows.size > OPEN_ROWS) {
            this.rows.delete(this.rows.keys().next().value as string);
        }
    }
}
